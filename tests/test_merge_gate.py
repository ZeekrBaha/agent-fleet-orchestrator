"""Tests for Task 6.1: conflict simulation + merge gate + squash.

TDD: all tests are written BEFORE the implementation exists.
Tests must fail (RED) before fleet/review/merge.py etc. are created.

Test groups:
  1. ConflictChecker unit tests (real git repos via GitRepoFactory)
  2. MergeLock concurrency test
  3. MergeService integration tests (gate checks + squash)
  4. API endpoint tests (POST /api/merge/{wt_id}, GET /api/merge/{wt_id}/check)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _no_auth() -> None:
    """Bypass token auth in tests."""
    return None


@pytest_asyncio.fixture()
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "merge_test.db"))
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
# DB helper — insert a test repo + agent + worktree atomically
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
    " owned_paths_json, status, created_at, task_id"
)

_SQL_WORKTREE = (
    "INSERT INTO worktrees"
    f" ({_SQL_COLS})"
    " VALUES (:id, :agent_id, :repo_id, :path, :branch,"
    "  'main', '[]', 'active', :now, :task_id)"
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
    task_id: str | None = None,
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
                "now": now,
                "task_id": task_id,
            },
        )
        conn.commit()

    await db.write(_write)


# ---------------------------------------------------------------------------
# 1. ConflictChecker — unit tests
# ---------------------------------------------------------------------------


def test_conflict_checker_clean(repo_factory: Any) -> None:
    """Two branches with non-overlapping changes — no conflict."""
    from fleet.review.conflict import ConflictChecker
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("clean_repo")
    # Add a file on main that feature branch won't touch
    (repo / "other.txt").write_text("other\n")
    _git(["add", "other.txt"], cwd=repo)
    _git(["commit", "-m", "add other"], cwd=repo)

    # Create feature branch with its own new file
    _git(["checkout", "-b", "feature"], cwd=repo)
    (repo / "feature.txt").write_text("feature content\n")
    _git(["add", "feature.txt"], cwd=repo)
    _git(["commit", "-m", "feature commit"], cwd=repo)

    _git(["checkout", "main"], cwd=repo)

    result = ConflictChecker().check(repo, "feature", "main")

    assert result.has_conflict is False
    assert result.conflict_files == []


def test_conflict_checker_conflict(repo_factory: Any) -> None:
    """Two branches with conflicting edits to same line — conflict reported."""
    from fleet.review.conflict import ConflictChecker

    # make_with_conflict_potential leaves main with a conflicting change;
    # feature branch has a conflicting change on file.txt
    repo = repo_factory.make_with_conflict_potential("conflict_repo")

    result = ConflictChecker().check(repo, "feature", "main")

    assert result.has_conflict is True
    assert result.summary != ""


# ---------------------------------------------------------------------------
# 2. MergeLock — concurrency tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_lock_concurrent() -> None:
    """Second acquire() on the same scope raises MergeInProgressError."""
    from fleet.review.lock import MergeInProgressError, MergeLock

    lock = MergeLock()
    results: list[str] = []

    async def hold_lock() -> None:
        async with lock.acquire("scope-A"):
            results.append("inside")
            await asyncio.sleep(0.05)

    async def try_lock() -> None:
        await asyncio.sleep(0.01)  # let hold_lock enter first
        with pytest.raises(MergeInProgressError) as exc_info:
            async with lock.acquire("scope-A"):
                results.append("should_not_reach")
        assert "scope-A" in str(exc_info.value)

    await asyncio.gather(hold_lock(), try_lock())
    assert "inside" in results
    assert "should_not_reach" not in results


@pytest.mark.asyncio
async def test_merge_lock_different_scopes() -> None:
    """Two concurrent acquire() calls on different scopes — both succeed."""
    from fleet.review.lock import MergeLock

    lock = MergeLock()
    results: list[str] = []

    async def hold_a() -> None:
        async with lock.acquire("scope-A"):
            results.append("A")
            await asyncio.sleep(0.02)

    async def hold_b() -> None:
        async with lock.acquire("scope-B"):
            results.append("B")
            await asyncio.sleep(0.02)

    await asyncio.gather(hold_a(), hold_b())
    assert "A" in results
    assert "B" in results


# ---------------------------------------------------------------------------
# 3. MergeService — integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_gate_no_evidence_rejected(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """MergeService raises MergeGateError when no evidence exists for the task."""
    from fleet.review.conflict import ConflictChecker
    from fleet.review.lock import MergeLock
    from fleet.review.merge import MergeGateError, MergeService
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_no_evidence")
    branch = "fleet/task-no-evidence"
    await evidence_svc.create_task(
        scope="test",
        title="my task",
        description="desc",
        branch=branch,
    )

    _git(["checkout", "-b", branch], cwd=repo)
    (repo / "newfile.txt").write_text("new content\n")
    _git(["add", "newfile.txt"], cwd=repo)
    _git(["commit", "-m", "task work"], cwd=repo)
    _git(["checkout", "main"], cwd=repo)

    # Use a plain directory — git status will return non-zero (not a repo),
    # and _git_porcelain handles that by returning "" (clean).
    wt_path = tmp_path / "worktree_no_evidence"
    wt_path.mkdir()

    worktree_id = str(uuid.uuid4())
    await _setup_worktree(
        db,
        repo_id="repo-001",
        repo_path=repo,
        agent_id="agent-001",
        worktree_id=worktree_id,
        worktree_path=wt_path,
        branch=branch,
    )

    merge_svc = MergeService(
        db=db,
        event_service=event_service,
        evidence_service=evidence_svc,
        conflict_checker=ConflictChecker(),
        lock=MergeLock(),
    )

    with pytest.raises(MergeGateError) as exc_info:
        await merge_svc.execute_merge(
            worktree_id=worktree_id,
            agent_id="agent-001",
            scope="test",
        )

    assert "merge gate" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_merge_gate_dirty_worktree_rejected(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """MergeService raises MergeGateError when worktree has uncommitted changes."""
    import shutil

    from fleet.review.conflict import ConflictChecker
    from fleet.review.lock import MergeLock
    from fleet.review.merge import MergeGateError, MergeService
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_dirty")
    branch = "fleet/task-dirty-test"
    await evidence_svc.create_task(
        scope="test",
        title="dirty task",
        description="desc",
        branch=branch,
    )
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT id FROM tasks WHERE branch = :b"),
            {"b": branch},
        ).fetchone()
    assert row is not None
    await evidence_svc.record_evidence(row.id, "pytest", "pass", "ok")

    _git(["checkout", "-b", branch], cwd=repo)
    (repo / "work.txt").write_text("done\n")
    _git(["add", "work.txt"], cwd=repo)
    _git(["commit", "-m", "work done"], cwd=repo)
    _git(["checkout", "main"], cwd=repo)

    # Build a dirty git worktree by copying the repo and staging a file
    wt_real = tmp_path / "wt_dirty_real"
    shutil.copytree(repo, wt_real)
    (wt_real / "dirty_uncommitted.txt").write_text("uncommitted\n")
    _git(["add", "dirty_uncommitted.txt"], cwd=wt_real)
    # Staged but not committed — git status --porcelain shows it

    worktree_id = str(uuid.uuid4())
    await _setup_worktree(
        db,
        repo_id="repo-dirty",
        repo_path=repo,
        agent_id="agent-dirty",
        worktree_id=worktree_id,
        worktree_path=wt_real,
        branch=branch,
    )

    merge_svc = MergeService(
        db=db,
        event_service=event_service,
        evidence_service=evidence_svc,
        conflict_checker=ConflictChecker(),
        lock=MergeLock(),
    )

    with pytest.raises(MergeGateError) as exc_info:
        await merge_svc.execute_merge(
            worktree_id=worktree_id,
            agent_id="agent-dirty",
            scope="test",
        )

    assert "uncommitted" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_merge_gate_conflict_rejected(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """Conflicting branches → ConflictError raised; default branch untouched."""
    import shutil

    from fleet.review.conflict import ConflictChecker
    from fleet.review.lock import MergeLock
    from fleet.review.merge import ConflictError, MergeService
    from fleet.workspace.gitops import git_run
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_with_conflict_potential("conflict_merge")

    await evidence_svc.create_task(
        scope="test",
        title="conflict task",
        description="desc",
        branch="feature",
    )
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT id FROM tasks WHERE branch = 'feature'"),
        ).fetchone()
    assert row is not None
    conflict_task_id = row.id

    # Make a clean copy of the repo on the feature branch (no uncommitted changes)
    wt_real = tmp_path / "wt_conflict"
    shutil.copytree(repo, wt_real)
    _git(["checkout", "feature"], cwd=wt_real)
    wt_sha = git_run(["git", "rev-parse", "HEAD"], cwd=wt_real).strip()
    await evidence_svc.record_evidence(
        conflict_task_id, "pytest", "pass", "ok", commit_sha=wt_sha
    )

    worktree_id = str(uuid.uuid4())
    await _setup_worktree(
        db,
        repo_id="repo-conflict",
        repo_path=repo,
        agent_id="agent-conflict",
        worktree_id=worktree_id,
        worktree_path=wt_real,
        branch="feature",
        task_id=conflict_task_id,
    )

    head_before = git_run(["git", "rev-parse", "HEAD"], cwd=repo)

    merge_svc = MergeService(
        db=db,
        event_service=event_service,
        evidence_service=evidence_svc,
        conflict_checker=ConflictChecker(),
        lock=MergeLock(),
    )

    with pytest.raises(ConflictError) as exc_info:
        await merge_svc.execute_merge(
            worktree_id=worktree_id,
            agent_id="agent-conflict",
            scope="test",
        )

    assert exc_info.value.conflict_result.has_conflict is True

    # Default branch must be untouched
    head_after = git_run(["git", "rev-parse", "HEAD"], cwd=repo)
    assert head_before == head_after


@pytest.mark.asyncio
async def test_merge_gate_success(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """Clean worktree + passing evidence → squash commit; merge_result event emitted."""
    from fleet.review.conflict import ConflictChecker
    from fleet.review.lock import MergeLock
    from fleet.review.merge import MergeResult, MergeService
    from fleet.workspace.gitops import git_run, worktree_add
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_success")
    branch = "fleet/task-success-test"

    await evidence_svc.create_task(
        scope="test",
        title="success task",
        description="desc",
        branch=branch,
    )
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT id FROM tasks WHERE branch = :b"),
            {"b": branch},
        ).fetchone()
    assert row is not None
    task_id = row.id

    wt_path = tmp_path / "wt_success"
    worktree_add(repo, wt_path, branch)
    (wt_path / "feature_file.txt").write_text("feature work\n")
    _git(["add", "feature_file.txt"], cwd=wt_path)
    _git(["commit", "-m", "add feature file"], cwd=wt_path)
    wt_sha = git_run(["git", "rev-parse", "HEAD"], cwd=wt_path).strip()
    await evidence_svc.record_evidence(
        task_id, "pytest", "pass", "all pass", commit_sha=wt_sha
    )

    worktree_id = str(uuid.uuid4())
    await _setup_worktree(
        db,
        repo_id="repo-success",
        repo_path=repo,
        agent_id="agent-success",
        worktree_id=worktree_id,
        worktree_path=wt_path,
        branch=branch,
        task_id=task_id,
    )

    merge_svc = MergeService(
        db=db,
        event_service=event_service,
        evidence_service=evidence_svc,
        conflict_checker=ConflictChecker(),
        lock=MergeLock(),
    )

    result = await merge_svc.execute_merge(
        worktree_id=worktree_id,
        agent_id="agent-success",
        scope="test",
        task_name="success task",
    )

    assert isinstance(result, MergeResult)
    assert result.commit_sha != ""
    assert result.branch == branch
    assert result.task_id == task_id

    # Commit message begins with feat: <task_id>
    commit_msg = git_run(["git", "log", "-1", "--format=%s"], cwd=repo)
    assert commit_msg.startswith(f"feat: {task_id}")

    # merge_result event emitted
    events = await event_service.query("test", type_filter="merge_result")
    assert len(events) >= 1
    last = events[-1]
    assert last.summary == "merged"
    assert last.payload["commit_sha"] == result.commit_sha

    # Worktree status updated to 'merged' in DB
    with db.read_connection() as conn:
        wt_row = conn.execute(
            text("SELECT status FROM worktrees WHERE id = :id"),
            {"id": worktree_id},
        ).fetchone()
    assert wt_row is not None
    assert wt_row.status == "merged"


# ---------------------------------------------------------------------------
# 4. API endpoint tests
# ---------------------------------------------------------------------------


def _build_merge_app(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
) -> FastAPI:
    """Build a minimal FastAPI app with the merge router wired in."""
    from fleet.api.auth import require_token
    from fleet.api.merge import router, set_merge_service
    from fleet.review.conflict import ConflictChecker
    from fleet.review.lock import MergeLock
    from fleet.review.merge import MergeService

    merge_svc = MergeService(
        db=db,
        event_service=event_service,
        evidence_service=evidence_svc,
        conflict_checker=ConflictChecker(),
        lock=MergeLock(),
    )
    set_merge_service(merge_svc)

    app = FastAPI()
    app.dependency_overrides[require_token] = _no_auth
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_merge_check_endpoint_dry_run(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """GET /api/merge/{wt_id}/check returns gate status without mutating anything."""
    from fleet.workspace.gitops import git_run, worktree_add
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_dryrun")
    branch = "fleet/task-dryrun-test"
    await evidence_svc.create_task(
        scope="test",
        title="dryrun task",
        description="desc",
        branch=branch,
    )
    # No evidence → gate should report can_merge=False

    wt_path = tmp_path / "wt_dryrun"
    worktree_add(repo, wt_path, branch)
    (wt_path / "dry.txt").write_text("dry\n")
    _git(["add", "dry.txt"], cwd=wt_path)
    _git(["commit", "-m", "dry work"], cwd=wt_path)

    worktree_id = str(uuid.uuid4())
    await _setup_worktree(
        db,
        repo_id="repo-dryrun",
        repo_path=repo,
        agent_id="agent-dryrun",
        worktree_id=worktree_id,
        worktree_path=wt_path,
        branch=branch,
    )

    head_before = git_run(["git", "rev-parse", "HEAD"], cwd=repo)

    app = _build_merge_app(db, event_service, evidence_svc)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        resp = await client.get(f"/api/merge/{worktree_id}/check")

    assert resp.status_code == 200
    body = resp.json()
    assert "can_merge" in body
    assert body["can_merge"] is False  # no evidence → gate fails
    assert "reason" in body

    # No mutation: HEAD unchanged
    head_after = git_run(["git", "rev-parse", "HEAD"], cwd=repo)
    assert head_before == head_after

    # No events emitted during dry run
    events = await event_service.query("test", type_filter="merge_result")
    assert events == []


@pytest.mark.asyncio
async def test_merge_endpoint_no_evidence_422(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """POST /api/merge/{wt_id} without evidence → 422 with merge gate detail."""
    from fleet.workspace.gitops import git_run, worktree_add
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_api_noev")
    branch = "fleet/task-api-noev"
    await evidence_svc.create_task(
        scope="test",
        title="api task",
        description="desc",
        branch=branch,
    )

    wt_path = tmp_path / "wt_api_noev"
    worktree_add(repo, wt_path, branch)
    (wt_path / "work.txt").write_text("work\n")
    _git(["add", "work.txt"], cwd=wt_path)
    _git(["commit", "-m", "work"], cwd=wt_path)

    worktree_id = str(uuid.uuid4())
    await _setup_worktree(
        db,
        repo_id="repo-api-noev",
        repo_path=repo,
        agent_id="agent-api-noev",
        worktree_id=worktree_id,
        worktree_path=wt_path,
        branch=branch,
    )

    app = _build_merge_app(db, event_service, evidence_svc)

    head_before = git_run(["git", "rev-parse", "HEAD"], cwd=repo)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        resp = await client.post(
            f"/api/merge/{worktree_id}",
            json={"agent_id": "agent-api-noev", "scope": "test"},
        )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "merge gate" in detail.lower()

    head_after = git_run(["git", "rev-parse", "HEAD"], cwd=repo)
    assert head_before == head_after, (
        "default branch HEAD must not change on gate failure"
    )


@pytest.mark.asyncio
async def test_merge_lock_concurrent_http(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
    repo_factory: Any,
    tmp_path: Any,
) -> None:
    """Two concurrent POST /api/merge requests on the same scope: one 200, one 409."""
    from fleet.workspace.gitops import git_run, worktree_add
    from tests.fixtures.gitrepo import _git

    repo = repo_factory.make_clean("repo_concurrent")
    branch = "fleet/task-concurrent-test"

    await evidence_svc.create_task(
        scope="concurrent-scope",
        title="concurrent task",
        description="desc",
        branch=branch,
    )
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT id FROM tasks WHERE branch = :b"),
            {"b": branch},
        ).fetchone()
    assert row is not None
    task_id = row.id

    wt_path = tmp_path / "wt_concurrent"
    worktree_add(repo, wt_path, branch)
    (wt_path / "feature_file.txt").write_text("feature work\n")
    _git(["add", "feature_file.txt"], cwd=wt_path)
    _git(["commit", "-m", "add feature file"], cwd=wt_path)
    wt_sha = git_run(["git", "rev-parse", "HEAD"], cwd=wt_path).strip()
    await evidence_svc.record_evidence(
        task_id, "pytest", "pass", "all pass", commit_sha=wt_sha
    )

    worktree_id = str(uuid.uuid4())
    await _setup_worktree(
        db,
        repo_id="repo-concurrent",
        repo_path=repo,
        agent_id="agent-concurrent",
        worktree_id=worktree_id,
        worktree_path=wt_path,
        branch=branch,
        task_id=task_id,
    )

    app = _build_merge_app(db, event_service, evidence_svc)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        payload = {"agent_id": "agent-concurrent", "scope": "concurrent-scope"}
        responses = await asyncio.gather(
            client.post(f"/api/merge/{worktree_id}", json=payload),
            client.post(f"/api/merge/{worktree_id}", json=payload),
            return_exceptions=False,
        )

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409], f"Expected [200, 409], got {statuses}"

    # Lock is now keyed by repo_path, so the 409 message includes the repo path.
    conflict_resp = next(r for r in responses if r.status_code == 409)
    assert "merge in progress" in conflict_resp.json()["detail"]


# ---------------------------------------------------------------------------
# A2: Evidence trust model — gate_require_reviewer tests (RED)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_self_evidence_gate_fails(db: DatabaseManager) -> None:
    """Worker-only evidence must not open the merge gate.

    A worker that records its own pytest=pass must not be able to merge.
    The gate should fail with a 'no reviewer verdict' reason when
    gate_require_reviewer=True (the default).
    """
    from fleet.review.evidence import EvidenceService

    svc = EvidenceService(db)
    task_id = await svc.create_task(
        scope="s1", title="t", description="d", owner_agent_id="worker-agent"
    )
    await svc.record_evidence(
        task_id,
        "pytest",
        "pass",
        "all green",
        recorded_by="worker-agent",
        recorded_by_role="worker",
    )

    can_merge, reason = await svc.check_merge_gate(task_id)

    assert not can_merge, "Gate should be closed — only worker self-evidence present"
    assert "reviewer" in reason.lower(), (
        f"Expected reason mentioning reviewer, got: {reason!r}"
    )


@pytest.mark.asyncio
async def test_reviewer_verdict_from_different_agent_opens_gate(
    db: DatabaseManager,
) -> None:
    """Reviewer evidence from a different agent must open the merge gate."""
    from fleet.review.evidence import EvidenceService

    svc = EvidenceService(db)
    task_id = await svc.create_task(
        scope="s2", title="t", description="d", owner_agent_id="worker-agent"
    )
    await svc.record_evidence(
        task_id,
        "pytest",
        "pass",
        "all green",
        recorded_by="worker-agent",
        recorded_by_role="worker",
    )
    await svc.record_evidence(
        task_id,
        "review",
        "pass",
        "looks good",
        recorded_by="reviewer-agent",
        recorded_by_role="reviewer",
    )

    can_merge, reason = await svc.check_merge_gate(task_id)

    assert can_merge, (
        f"Gate should be open — reviewer verdict present. reason={reason!r}"
    )


@pytest.mark.asyncio
async def test_reviewer_self_review_does_not_open_gate(db: DatabaseManager) -> None:
    """Reviewer reviewing their own worktree must not satisfy the gate."""
    from fleet.review.evidence import EvidenceService

    owner = "worker-who-is-also-reviewer"
    svc = EvidenceService(db)
    task_id = await svc.create_task(
        scope="s3", title="t", description="d", owner_agent_id=owner
    )
    await svc.record_evidence(
        task_id,
        "review",
        "pass",
        "self-approved",
        recorded_by=owner,        # same as task owner → self-review
        recorded_by_role="reviewer",
    )

    can_merge, reason = await svc.check_merge_gate(task_id)

    assert not can_merge, (
        "Gate should be closed — reviewer is the same agent as the task owner"
    )
    assert "reviewer" in reason.lower(), (
        f"Expected reason mentioning reviewer, got: {reason!r}"
    )


@pytest.mark.asyncio
async def test_gate_require_reviewer_false_allows_worker_evidence(
    db: DatabaseManager,
) -> None:
    """With gate_require_reviewer=False, worker-only pass evidence opens the gate."""
    from fleet.review.evidence import EvidenceService

    svc = EvidenceService(db, gate_require_reviewer=False)
    task_id = await svc.create_task(
        scope="s4", title="t", description="d", owner_agent_id="worker-agent"
    )
    await svc.record_evidence(
        task_id,
        "pytest",
        "pass",
        "all green",
        recorded_by="worker-agent",
        recorded_by_role="worker",
    )

    can_merge, reason = await svc.check_merge_gate(task_id)

    assert can_merge, (
        f"Gate should be open when gate_require_reviewer=False. reason={reason!r}"
    )
