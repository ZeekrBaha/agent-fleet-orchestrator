"""WorktreeService — git worktree management for the Fleet orchestrator.

Manages the ``worktrees`` table per ADR-006: one worktree per agent task,
sibling to the main repo, with dirty-repo gate and owned-path isolation.

Public API:
    DirtyRepoError                                 — raised when repo is dirty
    OverlapError                                   — raised on owned-path conflict
    WorktreeError                                  — raised on git worktree failure
    WorktreeService(db, event_service, workspace_service)
    WorktreeService.create_worktree(...)           -> WorktreeRecord
    WorktreeService.remove_worktree(worktree_id)   -> None
    WorktreeService.get_wip_report(worktree_id)    -> dict
    WorktreeService.list_worktrees(repo_id, ...)   -> list[WorktreeRecord]
    WorktreeService.handle_dirty_repo(...)         -> None
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Connection, text

from fleet.db import DatabaseManager
from fleet.events.service import EventService
from fleet.models import WorktreeRecord
from fleet.workspace.gitops import (
    GitError,
    get_worktree_status,
    git_run,
    is_repo_dirty,
    worktree_add,
    worktree_remove,
)

# WorkspaceService is guarded by TYPE_CHECKING to avoid circular imports;
# the actual instance is passed at construction time.
if TYPE_CHECKING:
    from fleet.workspace.service import WorkspaceService


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class DirtyRepoError(Exception):
    """Raised when a worktree creation is blocked by uncommitted changes."""

    def __init__(self, options: list[str]) -> None:
        self.options = options
        super().__init__(
            f"Repository has uncommitted changes. Options: {options}"
        )


class OverlapError(Exception):
    """Raised when owned_paths conflict with an existing active worktree."""

    def __init__(self, conflicting_worktree_id: str) -> None:
        self.conflicting_worktree_id = conflicting_worktree_id
        super().__init__(
            f"Owned paths overlap with worktree {conflicting_worktree_id!r}"
        )


class WorktreeError(Exception):
    """Raised when a git worktree operation fails."""

    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


MAX_SLUG_LEN = 60  # combined task_id-name; avoids 255-byte filesystem limit


def _paths_overlap(a: str, b: str) -> bool:
    """True if path patterns a and b could refer to the same file."""
    a_norm = a.rstrip("/")
    b_norm = b.rstrip("/")
    return (
        fnmatch.fnmatch(b_norm, a_norm)
        or fnmatch.fnmatch(a_norm, b_norm)
        or b_norm.startswith(a_norm + "/")
        or a_norm.startswith(b_norm + "/")
        or a_norm == b_norm
    )


def _slugify(task_id: str, name: str) -> str:
    """Convert task_id and name to a combined lowercase slug.

    The slug is capped at MAX_SLUG_LEN characters to avoid filesystem limits.

    Example: ("task-001", "My Feature") → "task-001-my-feature".
    """
    combined = f"{task_id}-{name}".lower()
    slug = re.sub(r"[^a-z0-9-]", "-", combined)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:MAX_SLUG_LEN] or "unnamed"


def _row_to_worktree_record(row: object) -> WorktreeRecord:
    """Convert a SQLAlchemy row to a WorktreeRecord."""
    return WorktreeRecord(
        id=row.id,  # type: ignore[attr-defined]
        agent_id=row.agent_id,  # type: ignore[attr-defined]
        repository_id=row.repository_id,  # type: ignore[attr-defined]
        path=row.path,  # type: ignore[attr-defined]
        branch=row.branch,  # type: ignore[attr-defined]
        base_branch=row.base_branch,  # type: ignore[attr-defined]
        owned_paths_json=row.owned_paths_json,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
    )


# ---------------------------------------------------------------------------
# WorktreeService
# ---------------------------------------------------------------------------


class WorktreeService:
    """Manages git worktrees for Fleet agents.

    Enforces:
    - Dirty-repo gate: raises DirtyRepoError when repo has uncommitted changes.
    - Ownership isolation: raises OverlapError on owned_paths conflict.
    - Cleanup-on-failure: no orphan worktree directories left on errors.
    """

    def __init__(
        self,
        db: DatabaseManager,
        event_service: EventService,
        workspace_service: WorkspaceService,
    ) -> None:
        self._db = db
        self._event_service = event_service
        self._workspace = workspace_service
        # Per-repo asyncio.Lock to serialize create_worktree calls (P1-19 TOCTOU fix).
        self._repo_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_worktree(
        self,
        repo_id: str,
        agent_id: str,
        task_id: str,
        name: str,
        owned_paths: list[str],
        *,
        skip_dirty_check: bool = False,
    ) -> WorktreeRecord:
        """Create a new git worktree for an agent task.

        Args:
            repo_id:          ID of the registered repository.
            agent_id:         ID of the owning agent (must exist in agents table).
            task_id:          Task identifier used in branch naming.
            name:             Human name for the worktree (slugified in branch).
            owned_paths:      File path globs this worktree owns (overlap check).
            skip_dirty_check: If True, skip the dirty-repo check (use after
                              handle_dirty_repo("continue_dirty", ...)).

        Returns:
            WorktreeRecord for the new worktree (status=active).

        Raises:
            ValueError:      If repo_id is not registered.
            DirtyRepoError:  If the repo has uncommitted changes and
                             skip_dirty_check is False.
            OverlapError:    If owned_paths overlap with an active worktree.
            WorktreeError:   If git worktree add fails.
        """
        repo = await self._workspace.get_repo(repo_id)
        if repo is None:
            raise ValueError(f"Repository not found: {repo_id!r}")

        if not skip_dirty_check and await asyncio.to_thread(is_repo_dirty, repo.path):
            raise DirtyRepoError(
                options=["continue_dirty", "stash", "commit", "cancel"]
            )

        # Serialize concurrent create_worktree calls per repo (P1-19 TOCTOU fix).
        # Without this lock, two concurrent calls could both pass the overlap check
        # (reading the same empty active list) then both insert, violating ownership
        # isolation.
        lock = self._repo_locks.setdefault(repo_id, asyncio.Lock())
        async with lock:
            # Overlap check against active worktrees.
            active = await self.list_worktrees(repo_id, status="active")
            for existing in active:
                existing_paths = json.loads(existing.owned_paths_json)
                for new_path in owned_paths:
                    for existing_path in existing_paths:
                        if _paths_overlap(new_path, existing_path):
                            raise OverlapError(conflicting_worktree_id=existing.id)

            # Build branch name: fleet/<slug> where slug = slugify(task_id, name)
            slug = _slugify(task_id, name)
            branch = f"fleet/{slug}"

            # Worktree lives sibling to the main repo:
            # <repo-parent>/fleet-worktrees/<branch>
            repo_path = Path(repo.path)
            worktree_path = repo_path.parent / "fleet-worktrees" / branch
            worktree_path.parent.mkdir(parents=True, exist_ok=True)

            base_branch = repo.default_branch
            try:
                await asyncio.to_thread(
                    worktree_add, repo.path, worktree_path, branch,
                    base_branch=base_branch,
                )
            except GitError as exc:
                raise WorktreeError(f"git worktree add failed: {exc}") from exc

            worktree_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            owned_json = json.dumps(owned_paths)

            def _write(conn: Connection) -> None:
                conn.execute(
                    text(
                        "INSERT INTO worktrees"
                        " (id, agent_id, repository_id, path, branch, base_branch,"
                        "  owned_paths_json, status, created_at, task_id)"
                        " VALUES"
                        " (:id, :agent_id, :repository_id, :path, :branch,"
                        "  :base_branch, :owned_paths_json, :status, :created_at,"
                        "  :task_id)"
                    ),
                    {
                        "id": worktree_id,
                        "agent_id": agent_id,
                        "repository_id": repo_id,
                        "path": str(worktree_path),
                        "branch": branch,
                        "base_branch": base_branch,
                        "owned_paths_json": owned_json,
                        "status": "active",
                        "created_at": now,
                        "task_id": task_id,
                    },
                )
                conn.commit()

            await self._db.write(_write)

            await self._event_service.append(
                "workspace",
                "git_action",
                f"Worktree created: {branch}",
                agent_id=agent_id,
                payload={
                    "action": "worktree_created",
                    "worktree_id": worktree_id,
                    "repo_id": repo_id,
                    "branch": branch,
                    "path": str(worktree_path),
                },
            )

            record = await self._get_worktree(worktree_id)
            assert record is not None, "just-inserted worktree should be retrievable"
            return record

    async def remove_worktree(self, worktree_id: str) -> None:
        """Remove a worktree from disk and mark it removed in the DB.

        Args:
            worktree_id: ID of the worktree to remove.

        Raises:
            ValueError:    If worktree_id is not found.
            WorktreeError: If git worktree remove fails.
        """
        record = await self._get_worktree(worktree_id)
        if record is None:
            raise ValueError(f"Worktree not found: {worktree_id!r}")

        repo = await self._workspace.get_repo(record.repository_id)
        if repo is None:
            raise ValueError(
                f"Repository {record.repository_id!r} not found"
                f" for worktree {worktree_id!r}"
            )

        try:
            await asyncio.to_thread(worktree_remove, repo.path, record.path)
        except GitError as exc:
            raise WorktreeError(f"git worktree remove failed: {exc}") from exc

        def _write(conn: Connection) -> None:
            conn.execute(
                text("UPDATE worktrees SET status = 'removed' WHERE id = :id"),
                {"id": worktree_id},
            )
            conn.commit()

        await self._db.write(_write)

        await self._event_service.append(
            "workspace",
            "git_action",
            f"Worktree removed: {record.branch}",
            payload={
                "action": "worktree_removed",
                "worktree_id": worktree_id,
                "repo_id": record.repository_id,
                "branch": record.branch,
            },
        )

    async def get_wip_report(self, worktree_id: str) -> dict[str, object]:
        """Return current status of a worktree without modifying it.

        Args:
            worktree_id: ID of the worktree.

        Returns:
            Dict with keys: ahead (int), dirty_files (list[str]), branch (str).

        Raises:
            ValueError: If worktree_id is not found.
        """
        record = await self._get_worktree(worktree_id)
        if record is None:
            raise ValueError(f"Worktree not found: {worktree_id!r}")

        return await asyncio.to_thread(get_worktree_status, record.path)

    async def list_worktrees(
        self, repo_id: str, *, status: str | None = None
    ) -> list[WorktreeRecord]:
        """List worktrees for a repository, optionally filtered by status.

        Args:
            repo_id: Repository ID to filter by.
            status:  Optional status filter ("active", "merged", "removed").

        Returns:
            List of WorktreeRecord (may be empty).
        """
        with self._db.read_connection() as conn:
            if status is not None:
                rows = conn.execute(
                    text(
                        "SELECT id, agent_id, repository_id, path, branch,"
                        " base_branch, owned_paths_json, status, created_at"
                        " FROM worktrees"
                        " WHERE repository_id = :repo_id AND status = :status"
                        " ORDER BY created_at ASC"
                    ),
                    {"repo_id": repo_id, "status": status},
                ).fetchall()
            else:
                rows = conn.execute(
                    text(
                        "SELECT id, agent_id, repository_id, path, branch,"
                        " base_branch, owned_paths_json, status, created_at"
                        " FROM worktrees"
                        " WHERE repository_id = :repo_id"
                        " ORDER BY created_at ASC"
                    ),
                    {"repo_id": repo_id},
                ).fetchall()
        return [_row_to_worktree_record(r) for r in rows]

    async def handle_dirty_repo(
        self,
        repo_id: str,
        option: str,
        *,
        commit_message: str | None = None,
    ) -> None:
        """Handle a dirty repo situation before retrying create_worktree.

        Options:
            continue_dirty: no-op — caller retries with skip_dirty_check=True.
            stash:          git stash the working tree changes.
            commit:         Stage all and commit with commit_message.
            cancel:         no-op — caller does not retry.

        Args:
            repo_id:        ID of the registered repository.
            option:         "continue_dirty" | "stash" | "commit" | "cancel".
            commit_message: Required when option="commit".

        Raises:
            ValueError:    If repo_id not found or commit missing for "commit".
            WorktreeError: If git stash/commit fails.
        """
        repo = await self._workspace.get_repo(repo_id)
        if repo is None:
            raise ValueError(f"Repository not found: {repo_id!r}")

        if option in ("continue_dirty", "cancel"):
            return  # No-op: caller decides whether to retry.

        if option == "stash":
            try:
                await asyncio.to_thread(git_run, ["git", "stash"], cwd=repo.path)
            except GitError as exc:
                raise WorktreeError(f"git stash failed: {exc}") from exc

            await self._event_service.append(
                "workspace",
                "git_action",
                f"Stashed dirty changes in repo: {repo.path}",
                payload={"action": "stash", "repo_id": repo_id},
            )
            return

        if option == "commit":
            if not commit_message:
                raise ValueError(
                    "commit_message is required when option='commit'"
                )

            try:
                await asyncio.to_thread(git_run, ["git", "add", "-A"], cwd=repo.path)
                await asyncio.to_thread(
                    git_run, ["git", "commit", "-m", commit_message], cwd=repo.path
                )
            except GitError as exc:
                raise WorktreeError(f"git commit failed: {exc}") from exc

            await self._event_service.append(
                "workspace",
                "git_action",
                f"User-committed dirty changes: {commit_message!r}",
                payload={
                    "action": "commit",
                    "repo_id": repo_id,
                    "message": commit_message,
                },
            )
            return

        raise ValueError(
            f"Unknown dirty-repo option {option!r}."
            " Valid: continue_dirty, stash, commit, cancel"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_worktree(self, worktree_id: str) -> WorktreeRecord | None:
        """Fetch a single worktree row by ID."""
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT id, agent_id, repository_id, path, branch,"
                    " base_branch, owned_paths_json, status, created_at"
                    " FROM worktrees WHERE id = :id"
                ),
                {"id": worktree_id},
            ).fetchone()
        if row is None:
            return None
        return _row_to_worktree_record(row)
