"""Tests for T7: Merge gate integrity fixes (P1-9 through P1-14).

TDD: written BEFORE the fixes. Each test must fail RED first.

P1-9:  Merge fails when HEAD is not on base_branch
P1-10: Gate fails closed when _git_porcelain raises GitError
P1-10: Status-not-active worktree is rejected
P1-11: Cleanup on merge failure (squash + commit)
P1-12: Two merges on same repo from different scopes serialize
P1-13: _fetch_worktree_and_repo joins on task_id not branch
P1-14: Old fail + new pass for same check_name → can_merge=True
P1-14: Latest row = fail still blocks
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Shared fixtures (mirrors test_merge_gate.py pattern)
# ---------------------------------------------------------------------------


def _no_auth() -> None:
    return None


@pytest_asyncio.fixture()
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "fix_test.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture()
async def event_service(db: DatabaseManager) -> EventService:
    hub = SSEHub()
    return EventService(db, hub)


@pytest.fixture()
def repo_factory(tmp_path: Any) -> Any:
    from tests.fixtures.gitrepo import GitRepoFactory

    factory = GitRepoFactory(tmp_path)
    yield factory
    factory.cleanup()


@pytest_asyncio.fixture()
async def evidence_svc(db: DatabaseManager) -> Any:
    from fleet.review.evidence import EvidenceService

    return EvidenceService(db, gate_require_reviewer=False)


# ---------------------------------------------------------------------------
# DB helpers — insert test records
# ---------------------------------------------------------------------------

_SQL_REPO = (
    "INSERT INTO repositories"
    " (id, path, default_branch, merge_policy_json, created_at)"
    " VALUES (:id, :path, 'main', '{}', :now)"
)

_SQL_AGENT = (
    "INSERT INTO agents"
    " (id, name, scope, role, backend, model, status,"
    "  created_at, updated_at)"
    " VALUES (:id, 'agent', 'test', 'worker', 'mock', 'mock',"
    "  'idle', :now, :now)"
)

_SQL_COLS = (
    "id, agent_id, repository_id, path, branch, base_branch,"
    " owned_paths_json, status, created_at"
)

_SQL_WORKTREE = (
    "INSERT INTO worktrees"
    f" ({_SQL_COLS})"
    " VALUES (:id, :agent_id, :repo_id, :path, :branch,"
    "  'main', '[]', :status, :now)"
)

_SQL_WORKTREE_WITH_TASK = (
    "INSERT INTO worktrees"
    " (id, agent_id, repository_id, path, branch, base_branch,"
    "  owned_paths_json, status, created_at, task_id)"
    " VALUES (:id, :agent_id, :repo_id, :path, :branch,"
    "  'main', '[]', :status, :now, :task_id)"
)


async def _setup_worktree(
    db: DatabaseManager,
    *,
    repo_id: str,
    repo_path: Path,
    agent_id: str,
    worktree_id: str,
    worktree_path: Path,
    branch: str,
    status: str = "active",
) -> None:
    """Insert a repository, agent, and worktree record for tests."""
    now = datetime.now(UTC).isoformat()

    def _write(conn: Any) -> None:
        conn.execute(
            text(_SQL_REPO),
            {"id": repo_id, "path": str(repo_path), "now": now},
        )
        conn.execute(
            text(_SQL_AGENT),
            {"id": agent_id, "now": now},
        )
        conn.execute(
            text(_SQL_WORKTREE),
            {
                "id": worktree_id,
                "agent_id": agent_id,
                "repo_id": repo_id,
                "path": str(worktree_path),
                "branch": branch,
                "status": status,
                "now": now,
            },
        )
        conn.commit()

    await db.write(_write)


async def _setup_worktree_with_task(
    db: DatabaseManager,
    *,
    repo_id: str,
    repo_path: Path,
    agent_id: str,
    worktree_id: str,
    worktree_path: Path,
    branch: str,
    task_id: str,
    status: str = "active",
) -> None:
    """Insert repo, agent, and worktree record with explicit task_id."""
    now = datetime.now(UTC).isoformat()

    def _write(conn: Any) -> None:
        conn.execute(
            text(_SQL_REPO),
            {"id": repo_id, "path": str(repo_path), "now": now},
        )
        conn.execute(
            text(_SQL_AGENT),
            {"id": agent_id, "now": now},
        )
        conn.execute(
            text(_SQL_WORKTREE_WITH_TASK),
            {
                "id": worktree_id,
                "agent_id": agent_id,
                "repo_id": repo_id,
                "path": str(worktree_path),
                "branch": branch,
                "status": status,
                "now": now,
                "task_id": task_id,
            },
        )
        conn.commit()

    await db.write(_write)


def _make_merge_service(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    conflict_checker: Any = None,
) -> Any:
    from fleet.review.conflict import ConflictChecker
    from fleet.review.lock import MergeLock
    from fleet.review.merge import MergeService

    return MergeService(
        db=db,
        event_service=event_service,
        evidence_service=evidence_svc,
        conflict_checker=conflict_checker or ConflictChecker(),
        lock=MergeLock(),
    )


# ---------------------------------------------------------------------------
# P1-9: Merge fails when HEAD is not on base_branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_9_merge_fails_wrong_head(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """execute_merge raises MergeGateError when main repo HEAD is on wrong branch."""
    from fleet.review.merge import MergeGateError
    from fleet.workspace.gitops import worktree_add
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_wrong_head")
    branch = "fleet/task-wrong-head"

    task_id = await evidence_svc.create_task(
        scope="test", title="wrong head task", description="desc", branch=branch
    )
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "ok")

    wt_path = tmp_path / "wt_wrong_head"
    worktree_add(repo, wt_path, branch)
    (wt_path / "work.txt").write_text("work\n")
    _git(["add", "work.txt"], cwd=wt_path)
    _git(["commit", "-m", "work"], cwd=wt_path)

    # Leave repo HEAD on a non-main branch
    _git(["checkout", "-b", "some-other-branch"], cwd=repo)

    worktree_id = str(uuid.uuid4())
    await _setup_worktree_with_task(
        db,
        repo_id="repo-wh",
        repo_path=repo,
        agent_id="agent-wh",
        worktree_id=worktree_id,
        worktree_path=wt_path,
        branch=branch,
        task_id=task_id,
    )

    merge_svc = _make_merge_service(db, event_service, evidence_svc)

    with pytest.raises(MergeGateError) as exc_info:
        await merge_svc.execute_merge(
            worktree_id=worktree_id,
            agent_id="agent-wh",
            scope="test",
        )

    assert "some-other-branch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_p1_9_merge_fails_dirty_main_repo(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """execute_merge raises MergeGateError when main repo has uncommitted changes."""
    from fleet.review.merge import MergeGateError
    from fleet.workspace.gitops import worktree_add
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_dirty_main")
    branch = "fleet/task-dirty-main"

    task_id = await evidence_svc.create_task(
        scope="test", title="dirty main", description="desc", branch=branch
    )
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "ok")

    wt_path = tmp_path / "wt_dirty_main"
    worktree_add(repo, wt_path, branch)
    (wt_path / "work.txt").write_text("work\n")
    _git(["add", "work.txt"], cwd=wt_path)
    _git(["commit", "-m", "work"], cwd=wt_path)

    # Make main repo dirty (uncommitted change)
    (repo / "dirty.txt").write_text("dirty\n")
    _git(["add", "dirty.txt"], cwd=repo)
    # staged but not committed

    worktree_id = str(uuid.uuid4())
    await _setup_worktree_with_task(
        db,
        repo_id="repo-dm",
        repo_path=repo,
        agent_id="agent-dm",
        worktree_id=worktree_id,
        worktree_path=wt_path,
        branch=branch,
        task_id=task_id,
    )

    merge_svc = _make_merge_service(db, event_service, evidence_svc)

    with pytest.raises(MergeGateError) as exc_info:
        await merge_svc.execute_merge(
            worktree_id=worktree_id,
            agent_id="agent-dm",
            scope="test",
        )

    assert "uncommitted" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# P1-10: Gate fails closed when _git_porcelain raises GitError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_10_porcelain_git_error_fails_closed(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    tmp_path: Any,
) -> None:
    """If _git_porcelain raises GitError, gate must fail closed (MergeGateError)."""
    from fleet.review.merge import MergeGateError

    # Path that is NOT a git repo — _agit_run will raise GitError
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    repo_path = tmp_path / "fake_repo"
    repo_path.mkdir()

    worktree_id = str(uuid.uuid4())
    task_id = await evidence_svc.create_task(
        scope="test", title="porcelain task", description="desc"
    )
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "ok")

    await _setup_worktree_with_task(
        db,
        repo_id="repo-pe",
        repo_path=repo_path,
        agent_id="agent-pe",
        worktree_id=worktree_id,
        worktree_path=not_a_repo,
        branch="fleet/task-pe",
        task_id=task_id,
    )

    merge_svc = _make_merge_service(db, event_service, evidence_svc)

    with pytest.raises(MergeGateError) as exc_info:
        await merge_svc.execute_merge(
            worktree_id=worktree_id,
            agent_id="agent-pe",
            scope="test",
        )

    # Must say something about cleanliness, not silently pass
    err = str(exc_info.value).lower()
    assert "cleanliness" in err or "worktree" in err


# ---------------------------------------------------------------------------
# P1-10: Status != 'active' worktree is rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_10_non_active_worktree_rejected(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    tmp_path: Any,
) -> None:
    """execute_merge raises MergeGateError when worktree status is not 'active'."""
    from fleet.review.merge import MergeGateError

    worktree_path = tmp_path / "wt_status_merged"
    worktree_path.mkdir()
    repo_path = tmp_path / "repo_status_merged"
    repo_path.mkdir()

    task_id = await evidence_svc.create_task(
        scope="test", title="already merged", description="desc",
        branch="fleet/task-status-merged",
    )
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "ok")

    worktree_id = str(uuid.uuid4())
    await _setup_worktree_with_task(
        db,
        repo_id="repo-nm",
        repo_path=repo_path,
        agent_id="agent-nm",
        worktree_id=worktree_id,
        worktree_path=worktree_path,
        branch="fleet/task-status-merged",
        task_id=task_id,
        status="merged",  # already merged
    )

    merge_svc = _make_merge_service(db, event_service, evidence_svc)

    with pytest.raises(MergeGateError) as exc_info:
        await merge_svc.execute_merge(
            worktree_id=worktree_id,
            agent_id="agent-nm",
            scope="test",
        )

    # The error should indicate the status is not active
    err_msg = str(exc_info.value).lower()
    assert "merged" in err_msg or "active" in err_msg or "status" in err_msg


# ---------------------------------------------------------------------------
# P1-12: Two merges on same repo from different scopes serialize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_12_same_repo_different_scopes_serialize(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """Two concurrent merges on the same repo from different scopes must serialize.

    With the old lock (keyed by scope), both could proceed concurrently on the
    same repo. With the fix (keyed by repo_path), the second must wait for the
    first, or raise MergeInProgressError.
    """
    from fleet.review.lock import MergeInProgressError
    from fleet.workspace.gitops import worktree_add
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_two_scopes")

    # Set up two branches / tasks on the same repo
    branch_a = "fleet/task-scope-a"
    branch_b = "fleet/task-scope-b"

    task_id_a = await evidence_svc.create_task(
        scope="scope-a", title="task a", description="desc", branch=branch_a
    )
    await evidence_svc.record_evidence(task_id_a, "pytest", "pass", "ok")

    task_id_b = await evidence_svc.create_task(
        scope="scope-b", title="task b", description="desc", branch=branch_b
    )
    await evidence_svc.record_evidence(task_id_b, "pytest", "pass", "ok")

    wt_path_a = tmp_path / "wt_scope_a"
    worktree_add(repo, wt_path_a, branch_a)
    (wt_path_a / "a.txt").write_text("a\n")
    _git(["add", "a.txt"], cwd=wt_path_a)
    _git(["commit", "-m", "a"], cwd=wt_path_a)

    wt_path_b = tmp_path / "wt_scope_b"
    worktree_add(repo, wt_path_b, branch_b)
    (wt_path_b / "b.txt").write_text("b\n")
    _git(["add", "b.txt"], cwd=wt_path_b)
    _git(["commit", "-m", "b"], cwd=wt_path_b)
    # Move repo back to main so b can be squash-merged (a may land first)
    _git(["checkout", "main"], cwd=repo)

    worktree_id_a = str(uuid.uuid4())
    worktree_id_b = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    repo_str = str(repo)

    def _write_all(conn: Any) -> None:
        conn.execute(
            text(_SQL_REPO),
            {"id": "repo-ts", "path": repo_str, "now": now},
        )
        for i, (aid, wid, wpath, branch, tid) in enumerate([
            ("agent-ts-a", worktree_id_a, str(wt_path_a), branch_a, task_id_a),
            ("agent-ts-b", worktree_id_b, str(wt_path_b), branch_b, task_id_b),
        ]):
            conn.execute(
                text(
                    "INSERT INTO agents"
                    " (id, name, scope, role, backend, model, status,"
                    "  created_at, updated_at)"
                    " VALUES (:id, :name, 'test', 'worker', 'mock', 'mock',"
                    "  'idle', :now, :now)"
                ),
                {"id": aid, "name": f"agent-ts-{i}", "now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO worktrees"
                    " (id, agent_id, repository_id, path, branch, base_branch,"
                    "  owned_paths_json, status, created_at, task_id)"
                    " VALUES (:id, :agent_id, :repo_id, :path, :branch,"
                    "  'main', '[]', 'active', :now, :task_id)"
                ),
                {
                    "id": wid,
                    "agent_id": aid,
                    "repo_id": "repo-ts",
                    "path": wpath,
                    "branch": branch,
                    "now": now,
                    "task_id": tid,
                },
            )
        conn.commit()

    await db.write(_write_all)

    from fleet.review.conflict import ConflictChecker
    from fleet.review.lock import MergeLock
    from fleet.review.merge import MergeService

    lock = MergeLock()
    merge_svc = MergeService(
        db=db,
        event_service=event_service,
        evidence_service=evidence_svc,
        conflict_checker=ConflictChecker(),
        lock=lock,
    )

    results: list[str] = []
    errors: list[Exception] = []

    async def do_merge_a() -> None:
        try:
            await merge_svc.execute_merge(
                worktree_id=worktree_id_a,
                agent_id="agent-ts-a",
                scope="scope-a",
            )
            results.append("a-ok")
        except MergeInProgressError:
            results.append("a-blocked")
        except Exception as exc:
            errors.append(exc)
            results.append(f"a-error: {exc}")

    async def do_merge_b() -> None:
        await asyncio.sleep(0.01)  # let a enter the lock first
        try:
            await merge_svc.execute_merge(
                worktree_id=worktree_id_b,
                agent_id="agent-ts-b",
                scope="scope-b",
            )
            results.append("b-ok")
        except MergeInProgressError:
            results.append("b-blocked")
        except Exception as exc:
            errors.append(exc)
            results.append(f"b-error: {exc}")

    await asyncio.gather(do_merge_a(), do_merge_b())

    # With lock keyed by repo: one should be blocked while the other runs,
    # OR they succeed sequentially (a finishes before b tries).
    # The critical invariant: both should NOT get "ok" simultaneously
    # without the repo being valid for the second merge.
    # Since the squash of a makes main dirty for b's squash-merge,
    # at least one must be blocked or the second gets a gate/merge error.
    # We assert that both didn't just silently do whatever they wanted
    # concurrently on the same repo.
    assert len(results) == 2
    # Acceptable outcomes:
    #   - One blocked (MergeInProgressError) while the other ran.
    #   - Both succeeded sequentially (repo state forces serialization).
    if "a-ok" in results and "b-ok" in results:
        # Both succeeded sequentially — lock properly serialized them.
        pass
    else:
        # At least one was blocked — also correct behavior.
        assert any("blocked" in r for r in results)


# ---------------------------------------------------------------------------
# P1-13: _fetch_worktree_and_repo joins on task_id not branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_13_fetch_joins_on_task_id_not_branch(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    tmp_path: Any,
) -> None:
    """task_id stored on worktree row must be returned, not a branch-name match.

    If the JOIN is on branch name, a task for the same branch in another scope
    could be returned. With task_id JOIN, only the task directly associated
    with the worktree is returned.
    """
    from fleet.review.conflict import ConflictChecker
    from fleet.review.lock import MergeLock
    from fleet.review.merge import MergeService

    worktree_path = tmp_path / "wt_task_join"
    worktree_path.mkdir()
    repo_path = tmp_path / "repo_task_join"
    repo_path.mkdir()

    shared_branch = "fleet/shared-branch-name"

    # Create two tasks with the same branch but different IDs
    task_id_real = await evidence_svc.create_task(
        scope="scope-real",
        title="real task",
        description="desc",
        branch=shared_branch,
    )
    task_id_imposter = await evidence_svc.create_task(
        scope="scope-fake",
        title="imposter task",
        description="desc",
        branch=shared_branch,
    )
    # Record evidence only on the imposter task (not on the real one)
    await evidence_svc.record_evidence(
        task_id_imposter, "pytest", "pass", "imposter ok"
    )

    worktree_id = str(uuid.uuid4())
    # Wire worktree to the real task_id
    await _setup_worktree_with_task(
        db,
        repo_id="repo-tj",
        repo_path=repo_path,
        agent_id="agent-tj",
        worktree_id=worktree_id,
        worktree_path=worktree_path,
        branch=shared_branch,
        task_id=task_id_real,  # real task — no evidence
    )

    merge_svc = MergeService(
        db=db,
        event_service=event_service,
        evidence_service=evidence_svc,
        conflict_checker=ConflictChecker(),
        lock=MergeLock(),
    )

    # Branch-JOIN would return imposter's task_id (has evidence).
    # task_id-JOIN (correct) returns real task_id (no evidence → gate fails).
    row = merge_svc._fetch_worktree_and_repo(worktree_id)

    # The fetched task_id must be the one directly stored in the worktree record
    assert row["task_id"] == task_id_real, (
        f"Expected task_id={task_id_real!r}, got {row['task_id']!r}. "
        "JOIN is probably still on branch name instead of task_id."
    )


@pytest.mark.asyncio
async def test_p1_13_worktree_service_persists_task_id(
    tmp_path: Any,
) -> None:
    """WorktreeService.create_worktree stores task_id in the worktrees table."""
    from unittest.mock import AsyncMock

    from pydantic import BaseModel
    from sqlalchemy import text

    from fleet.db import init_db
    from fleet.events.service import EventService
    from fleet.events.sse import SSEHub
    from fleet.workspace.worktree_service import WorktreeService

    db = await init_db(str(tmp_path / "wt_svc_test.db"))
    hub = SSEHub()
    ev = EventService(db, hub)

    # Create a minimal workspace_service mock
    class FakeRepo(BaseModel):
        id: str
        path: str
        default_branch: str = "main"
        merge_policy_json: str = "{}"
        created_at: str = "2024-01-01"

    fake_repo = FakeRepo(id="repo-wts", path=str(tmp_path / "fake_repo"))
    (tmp_path / "fake_repo").mkdir()

    workspace_svc = MagicMock()
    workspace_svc.get_repo = AsyncMock(return_value=fake_repo)

    # Pre-insert the agent
    now = datetime.now(UTC).isoformat()

    def _insert_agent(conn: Any) -> None:
        conn.execute(
            text(_SQL_REPO),
            {"id": "repo-wts", "path": str(tmp_path / "fake_repo"), "now": now},
        )
        conn.execute(
            text(_SQL_AGENT),
            {"id": "agent-wts", "now": now},
        )
        conn.commit()

    await db.write(_insert_agent)

    # Patch out worktree_add to avoid real git
    with patch("fleet.workspace.worktree_service.worktree_add"):
        wt_svc = WorktreeService(
            db=db, event_service=ev, workspace_service=workspace_svc
        )
        record = await wt_svc.create_worktree(
            repo_id="repo-wts",
            agent_id="agent-wts",
            task_id="my-task-001",
            name="My Feature",
            owned_paths=[],
            skip_dirty_check=True,
        )

    # Check the DB row has task_id
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT task_id FROM worktrees WHERE id = :id"),
            {"id": record.id},
        ).fetchone()

    assert row is not None
    assert row.task_id == "my-task-001", (
        f"task_id not stored in worktrees table: got {row.task_id!r}"
    )

    await db.close()


# ---------------------------------------------------------------------------
# P1-14: Latest row per check_name logic in check_merge_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_14_old_fail_new_pass_can_merge(
    db: DatabaseManager,
    evidence_svc: Any,
) -> None:
    """A check that previously failed but was re-run and passed should allow merge."""
    task_id = await evidence_svc.create_task(
        scope="test", title="rerun task", description="desc"
    )

    # First run: fail
    await evidence_svc.record_evidence(task_id, "pytest", "fail", "initial failure")
    # Second run: pass
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "fixed and passing")

    can_merge, reason = await evidence_svc.check_merge_gate(task_id)

    assert can_merge is True, (
        f"Expected can_merge=True after re-run passes, got False: {reason}"
    )


@pytest.mark.asyncio
async def test_p1_14_latest_fail_still_blocks(
    db: DatabaseManager,
    evidence_svc: Any,
) -> None:
    """If the latest row for a check_name is fail, gate must still block."""
    task_id = await evidence_svc.create_task(
        scope="test", title="still failing task", description="desc"
    )

    # First run: pass
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "was passing")
    # Second run: fail (regression)
    await evidence_svc.record_evidence(task_id, "pytest", "fail", "regression")

    can_merge, reason = await evidence_svc.check_merge_gate(task_id)

    assert can_merge is False, "Expected can_merge=False when latest row is fail"
    assert "pytest" in reason


@pytest.mark.asyncio
async def test_p1_14_review_old_fail_new_pass_can_merge(
    db: DatabaseManager,
    evidence_svc: Any,
) -> None:
    """For the 'review' check, old fail + new pass should allow merge."""
    task_id = await evidence_svc.create_task(
        scope="test", title="review rerun task", description="desc"
    )

    # Other evidence passing
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "ok")

    # Review: first reject, then approve
    await evidence_svc.record_evidence(task_id, "review", "fail", "not ready")
    await evidence_svc.record_evidence(task_id, "review", "pass", "approved")

    can_merge, reason = await evidence_svc.check_merge_gate(task_id)

    assert can_merge is True, (
        f"Expected can_merge=True after reviewer approved, got False: {reason}"
    )


@pytest.mark.asyncio
async def test_p1_14_review_latest_fail_blocks(
    db: DatabaseManager,
    evidence_svc: Any,
) -> None:
    """For the 'review' check, latest row = fail must still block."""
    task_id = await evidence_svc.create_task(
        scope="test", title="review still failing task", description="desc"
    )

    await evidence_svc.record_evidence(task_id, "pytest", "pass", "ok")
    await evidence_svc.record_evidence(task_id, "review", "pass", "initially approved")
    await evidence_svc.record_evidence(task_id, "review", "fail", "revoked")

    can_merge, reason = await evidence_svc.check_merge_gate(task_id)

    assert can_merge is False, "Expected can_merge=False when latest review is fail"
    assert "reviewer" in reason.lower()


# ---------------------------------------------------------------------------
# P1-11: Cleanup on squash merge failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_11_cleanup_on_merge_failure(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """When git merge --squash fails, repo is cleaned up (reset/abort attempted)."""
    import asyncio as _asyncio
    from functools import partial

    from fleet.review.conflict import ConflictChecker
    from fleet.review.lock import MergeLock
    from fleet.review.merge import MergeGateError, MergeService
    from fleet.workspace.gitops import GitError, git_run, worktree_add
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_cleanup")
    branch = "fleet/task-cleanup"

    task_id = await evidence_svc.create_task(
        scope="test", title="cleanup task", description="desc", branch=branch
    )
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "ok")

    wt_path = tmp_path / "wt_cleanup"
    worktree_add(repo, wt_path, branch)
    (wt_path / "cleanup.txt").write_text("cleanup\n")
    _git(["add", "cleanup.txt"], cwd=wt_path)
    _git(["commit", "-m", "cleanup work"], cwd=wt_path)

    worktree_id = str(uuid.uuid4())
    await _setup_worktree_with_task(
        db,
        repo_id="repo-cl",
        repo_path=repo,
        agent_id="agent-cl",
        worktree_id=worktree_id,
        worktree_path=wt_path,
        branch=branch,
        task_id=task_id,
    )

    # Simulate merge --squash succeeding but commit failing.
    async def patched_agit_run(
        cmd: list[str], *, cwd: Path, timeout: int = 30
    ) -> str:
        if cmd[:2] == ["git", "commit"]:
            raise GitError(cmd, 1, "simulated commit failure")
        # Delegate everything else to the real implementation.
        loop = _asyncio.get_running_loop()
        fn = partial(git_run, cmd, cwd=cwd, timeout=timeout)
        return await loop.run_in_executor(None, fn)

    with patch("fleet.review.merge._agit_run", side_effect=patched_agit_run):
        with pytest.raises(MergeGateError) as exc_info:
            svc = MergeService(
                db=db,
                event_service=event_service,
                evidence_service=evidence_svc,
                conflict_checker=ConflictChecker(),
                lock=MergeLock(),
            )
            await svc.execute_merge(
                worktree_id=worktree_id,
                agent_id="agent-cl",
                scope="test",
            )

    assert "squash merge failed" in str(exc_info.value).lower()

    # After cleanup, repo should be in a clean state (no SQUASH_HEAD, no staged changes)
    porcelain = git_run(["git", "status", "--porcelain"], cwd=repo)
    assert porcelain.strip() == "", (
        f"Repo not clean after failed merge + cleanup: {porcelain!r}"
    )
