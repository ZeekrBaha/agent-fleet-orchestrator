"""MergeService — conflict check, merge gate, and squash merge for Fleet.

Public API:
    MergeResult         — Pydantic model: commit_sha, branch, task_id
    MergeGateError      — raised when a gate check fails (→ 422)
    ConflictError       — raised when simulate_merge detects conflicts (→ 422)
    MergeService        — orchestrates gate checks + squash merge
    MergeService.execute_merge(worktree_id, agent_id, scope) -> MergeResult
    MergeService.check_gate(worktree_id) -> GateStatus
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import text

from fleet.db import DatabaseManager
from fleet.events.service import EventService
from fleet.review.conflict import ConflictChecker, ConflictResult
from fleet.review.evidence import EvidenceService
from fleet.review.lock import MergeLock
from fleet.workspace.gitops import GitError, git_run, worktree_remove


async def _agit_run(cmd: list[str], *, cwd: Path, timeout: int = 30) -> str:
    """Run git_run() in a thread-pool executor to avoid blocking the event loop."""
    loop = asyncio.get_running_loop()
    fn = partial(git_run, cmd, cwd=cwd, timeout=timeout)
    return await loop.run_in_executor(None, fn)


class MergeResult(BaseModel):
    """Returned on a successful squash merge."""

    commit_sha: str
    branch: str
    task_id: str


@dataclass
class GateStatus:
    """Result of a dry-run gate check (no mutations)."""

    can_merge: bool
    reason: str
    has_conflict: bool
    conflict_summary: str


class MergeGateError(Exception):
    """Raised when a merge gate check fails.

    Maps to HTTP 422 in the route handler.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"merge gate: {reason}")


class ConflictError(Exception):
    """Raised when a conflict simulation finds conflicts.

    Maps to HTTP 422 in the route handler.
    """

    def __init__(self, conflict_result: ConflictResult) -> None:
        self.conflict_result = conflict_result
        super().__init__(
            f"merge conflict detected: {conflict_result.summary[:200]}"
        )


class MergeService:
    """Orchestrates gate checks and squash merges."""

    def __init__(
        self,
        *,
        db: DatabaseManager,
        event_service: EventService,
        evidence_service: EvidenceService,
        conflict_checker: ConflictChecker,
        lock: MergeLock,
    ) -> None:
        self._db = db
        self._event_service = event_service
        self._evidence_service = evidence_service
        self._conflict_checker = conflict_checker
        self._lock = lock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_merge(
        self,
        worktree_id: str,
        agent_id: str,
        scope: str,
        *,
        task_name: str | None = None,
    ) -> MergeResult:
        """Run all gate checks then perform a squash merge.

        Steps (in order):
          1. Acquire scope lock (raises MergeInProgressError on contention → 409)
          2. Look up worktree + repository records
          3. Check worktree cleanliness
          4. Check evidence gate
          5. Check for conflicts (simulate_merge)
          6. Squash merge + commit on default branch
          7. Remove worktree + update DB status
          8. Emit merge_result event

        Args:
            worktree_id: ID of the worktree record to merge.
            agent_id:    Agent performing the merge (recorded in commit message).
            scope:       Scope for events and lock key.
            task_name:   Optional override for the task name in the commit message.
                         If None, fetched from the tasks table.

        Returns:
            MergeResult with commit_sha, branch, task_id.

        Raises:
            MergeInProgressError: Another merge is already running for this scope.
            MergeGateError:       A gate check failed.
            ConflictError:        Simulated merge detected conflicts.
            ValueError:           worktree_id or repository not found.
        """
        async with self._lock.acquire(scope):
            return await self._do_merge(
                worktree_id=worktree_id,
                agent_id=agent_id,
                scope=scope,
                task_name=task_name,
            )

    async def check_gate(self, worktree_id: str) -> GateStatus:
        """Dry-run gate check — no mutations, no events.

        Runs gate checks 1-3 and returns a GateStatus.  The default branch
        and worktree are never modified.

        Args:
            worktree_id: ID of the worktree record to check.

        Returns:
            GateStatus with can_merge, reason, has_conflict, conflict_summary.

        Raises:
            ValueError: worktree_id or repository not found.
        """
        row = self._fetch_worktree_and_repo(worktree_id)
        worktree_path = Path(row["worktree_path"])
        repo_path = Path(row["repo_path"])
        branch = row["branch"]
        base_branch = row["base_branch"]
        task_id = row["task_id"]

        # Gate 1: worktree cleanliness
        porcelain = await _git_porcelain(worktree_path)
        if porcelain:
            return GateStatus(
                can_merge=False,
                reason="worktree has uncommitted changes",
                has_conflict=False,
                conflict_summary="",
            )

        # Gate 2: evidence gate
        if task_id:
            can_merge, reason = await self._evidence_service.check_merge_gate(task_id)
            if not can_merge:
                return GateStatus(
                    can_merge=False,
                    reason=reason,
                    has_conflict=False,
                    conflict_summary="",
                )
        else:
            return GateStatus(
                can_merge=False,
                reason="no task associated with this worktree",
                has_conflict=False,
                conflict_summary="",
            )

        # Gate 3: conflict simulation
        loop = asyncio.get_running_loop()
        conflict_result = await loop.run_in_executor(
            None,
            partial(self._conflict_checker.check, repo_path, branch, base_branch),
        )
        if conflict_result.has_conflict:
            return GateStatus(
                can_merge=False,
                reason="merge conflict detected",
                has_conflict=True,
                conflict_summary=conflict_result.summary,
            )

        return GateStatus(
            can_merge=True,
            reason="all checks passed",
            has_conflict=False,
            conflict_summary="",
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_worktree_and_repo(self, worktree_id: str) -> dict[str, str]:
        """Return worktree + repository data as a dict.

        Returns keys: worktree_path, repo_path, branch, base_branch, task_id.
        task_id may be empty string if no task record has this branch.

        Raises:
            ValueError: If worktree_id is not found.
        """
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT w.path AS worktree_path, r.path AS repo_path,"
                    " w.branch, w.base_branch, COALESCE(t.id, '') AS task_id"
                    " FROM worktrees w"
                    " JOIN repositories r ON w.repository_id = r.id"
                    " LEFT JOIN tasks t ON t.branch = w.branch"
                    " WHERE w.id = :id"
                ),
                {"id": worktree_id},
            ).fetchone()

        if row is None:
            raise ValueError(f"Worktree not found: {worktree_id!r}")

        return {
            "worktree_path": row.worktree_path,
            "repo_path": row.repo_path,
            "branch": row.branch,
            "base_branch": row.base_branch,
            "task_id": row.task_id,
        }

    async def _do_merge(
        self,
        *,
        worktree_id: str,
        agent_id: str,
        scope: str,
        task_name: str | None,
    ) -> MergeResult:
        """Inner merge logic — called while the scope lock is held."""
        row = self._fetch_worktree_and_repo(worktree_id)
        worktree_path = Path(row["worktree_path"])
        repo_path = Path(row["repo_path"])
        branch = row["branch"]
        base_branch = row["base_branch"]
        task_id = row["task_id"]

        # Gate 1: worktree cleanliness
        porcelain = await _git_porcelain(worktree_path)
        if porcelain:
            raise MergeGateError("worktree has uncommitted changes")

        # Gate 2: evidence gate
        if not task_id:
            raise MergeGateError("no task associated with this worktree")

        can_merge, reason = await self._evidence_service.check_merge_gate(task_id)
        if not can_merge:
            raise MergeGateError(reason)

        # Gate 3: conflict simulation — emit git_action event first
        await self._event_service.append(
            scope,
            "git_action",
            "conflict_check",
            agent_id=agent_id,
            payload={
                "worktree_id": worktree_id,
                "branch": branch,
                "target": base_branch,
            },
        )

        loop = asyncio.get_running_loop()
        conflict_result = await loop.run_in_executor(
            None,
            partial(self._conflict_checker.check, repo_path, branch, base_branch),
        )
        if conflict_result.has_conflict:
            await self._event_service.append(
                scope,
                "merge_result",
                "conflict",
                agent_id=agent_id,
                payload={
                    "worktree_id": worktree_id,
                    "branch": branch,
                    "conflict_summary": conflict_result.summary,
                    "conflict_files": conflict_result.conflict_files,
                },
            )
            raise ConflictError(conflict_result)

        # Gate 4 hook (approvals — reserved for future; skip for now)

        # Resolve task name for commit message
        resolved_name = task_name or await self._fetch_task_title(task_id)

        # Step 6: squash merge on the main repo (not the worktree)
        await _agit_run(["git", "merge", "--squash", branch], cwd=repo_path)
        commit_msg = f"feat: {task_id} {resolved_name}\n\n[fleet] merged by {agent_id}"
        await _agit_run(["git", "commit", "-m", commit_msg], cwd=repo_path)
        commit_sha = await _agit_run(["git", "rev-parse", "HEAD"], cwd=repo_path)

        # Step 7: remove worktree + update DB status
        loop = asyncio.get_running_loop()
        try:
            fn = partial(worktree_remove, repo_path, worktree_path)
            await loop.run_in_executor(None, fn)
        except (OSError, GitError) as exc:
            # Non-fatal: squash commit already landed on default branch;
            # worktree directory cleanup fails don't roll back the merge.
            import warnings

            warnings.warn(
                f"worktree_remove failed (non-fatal): {exc}",
                stacklevel=2,
            )

        await self._update_worktree_status(worktree_id, "merged")

        # Step 8: emit merge_result event
        await self._event_service.append(
            scope,
            "merge_result",
            "merged",
            agent_id=agent_id,
            payload={
                "commit_sha": commit_sha,
                "branch": branch,
                "task_id": task_id,
                "worktree_id": worktree_id,
            },
        )

        return MergeResult(commit_sha=commit_sha, branch=branch, task_id=task_id)

    async def _fetch_task_title(self, task_id: str) -> str:
        """Return the task title for the commit message, or '' if not found."""
        task = await self._evidence_service.get_task(task_id)
        if task is None:
            return ""
        title = task.get("title", "")
        return str(title)

    async def _update_worktree_status(self, worktree_id: str, status: str) -> None:
        """Update worktree status in the DB."""
        from sqlalchemy import Connection

        def _write(conn: Connection) -> None:
            conn.execute(
                text("UPDATE worktrees SET status = :status WHERE id = :id"),
                {"status": status, "id": worktree_id},
            )
            conn.commit()

        await self._db.write(_write)


async def _git_porcelain(worktree_path: Path) -> str:
    """Return git status --porcelain output for *worktree_path*, or '' if not a repo."""
    try:
        return await _agit_run(["git", "status", "--porcelain"], cwd=worktree_path)
    except GitError:
        return ""
