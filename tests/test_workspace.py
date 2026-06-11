"""Tests for WorkspaceService (Task 3.1) — written BEFORE implementation exists.

TDD: all tests must FAIL before fleet/workspace/ modules are created.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_workspace.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def hub() -> SSEHub:
    return SSEHub()


@pytest_asyncio.fixture
async def event_service(db: DatabaseManager, hub: SSEHub) -> EventService:
    from fleet.events.service import create_event_service

    return create_event_service(db, hub)


@pytest_asyncio.fixture
async def workspace_service(db: DatabaseManager, event_service: EventService):
    from fleet.workspace.service import WorkspaceService

    return WorkspaceService(db, event_service)


@pytest.fixture
def repo_factory(tmp_path):
    from tests.fixtures.gitrepo import GitRepoFactory

    factory = GitRepoFactory(tmp_path)
    yield factory
    factory.cleanup()


# ---------------------------------------------------------------------------
# Fixture tests (GitRepoFactory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitrepo_factory_make_clean(repo_factory) -> None:
    """make_clean() returns a path that exists, has .git, and is not dirty."""
    from fleet.workspace.gitops import is_repo_dirty

    path = repo_factory.make_clean()

    assert path.exists(), "clean repo path should exist"
    assert (path / ".git").exists(), "clean repo should have .git directory"
    assert not is_repo_dirty(path), "clean repo should not be dirty"


@pytest.mark.asyncio
async def test_gitrepo_factory_make_dirty(repo_factory) -> None:
    """make_dirty() returns a path where is_repo_dirty() is True."""
    from fleet.workspace.gitops import is_repo_dirty

    path = repo_factory.make_dirty()

    assert path.exists()
    assert (path / ".git").exists()
    assert is_repo_dirty(path), "dirty repo should report dirty"


@pytest.mark.asyncio
async def test_gitrepo_factory_make_ahead(repo_factory) -> None:
    """make_ahead(commits=3) returns a path with 3 commits beyond the initial."""
    import subprocess

    path = repo_factory.make_ahead(commits=3)

    assert path.exists()
    assert (path / ".git").exists()

    # Count commits: should be initial + 3 = 4 total
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    total_commits = int(result.stdout.strip())
    assert total_commits == 4, f"expected 4 total commits, got {total_commits}"


# ---------------------------------------------------------------------------
# Registry tests (WorkspaceService)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_repo_returns_record(
    workspace_service, repo_factory
) -> None:
    """register_repo() returns a RepositoryRecord with correct path and branch."""
    path = repo_factory.make_clean()

    record = await workspace_service.register_repo(str(path))

    assert record.id != ""
    assert record.path == str(path)
    assert record.default_branch in ("main", "master", "trunk")
    assert isinstance(record.merge_policy, dict)
    assert record.created_at != ""


@pytest.mark.asyncio
async def test_register_repo_emits_event(
    workspace_service, repo_factory, event_service: EventService
) -> None:
    """After register_repo(), events table contains a state_change event."""
    path = repo_factory.make_clean()

    record = await workspace_service.register_repo(str(path))

    events = await event_service.query(
        "workspace",
        type_filter="state_change",
    )
    assert len(events) >= 1
    payloads = [e.payload for e in events]
    repo_ids = [p.get("repo_id") for p in payloads]
    assert record.id in repo_ids, "state_change event should carry repo_id"


@pytest.mark.asyncio
async def test_register_invalid_path_raises(workspace_service) -> None:
    """register_repo() raises ValueError for a non-existent path."""
    with pytest.raises(ValueError, match="does not exist"):
        await workspace_service.register_repo("/tmp/fleet_nonexistent_xyz_12345")


@pytest.mark.asyncio
async def test_register_non_git_path_raises(workspace_service, tmp_path) -> None:
    """register_repo() raises ValueError for a path that exists but has no .git."""
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()

    with pytest.raises(ValueError, match="not a git repository"):
        await workspace_service.register_repo(str(non_git))


@pytest.mark.asyncio
async def test_register_duplicate_path_raises(
    workspace_service, repo_factory
) -> None:
    """register_repo() raises ValueError if the same path is registered twice."""
    path = repo_factory.make_clean("dup")

    await workspace_service.register_repo(str(path))

    with pytest.raises(ValueError, match="already registered"):
        await workspace_service.register_repo(str(path))


@pytest.mark.asyncio
async def test_get_repo_by_path(workspace_service, repo_factory) -> None:
    """get_repo_by_path() returns the registered record for a known path."""
    path = repo_factory.make_clean("bypath")

    registered = await workspace_service.register_repo(str(path))
    found = await workspace_service.get_repo_by_path(str(path))

    assert found is not None
    assert found.id == registered.id
    assert found.path == str(path)


@pytest.mark.asyncio
async def test_list_repos(workspace_service, repo_factory) -> None:
    """list_repos() returns all registered repos."""
    path_a = repo_factory.make_clean("repo-a")
    path_b = repo_factory.make_clean("repo-b")

    await workspace_service.register_repo(str(path_a))
    await workspace_service.register_repo(str(path_b))

    repos = await workspace_service.list_repos()
    paths = [r.path for r in repos]

    assert str(path_a) in paths
    assert str(path_b) in paths
    assert len(repos) >= 2


@pytest.mark.asyncio
async def test_unregister_repo(workspace_service, repo_factory) -> None:
    """unregister_repo() removes the record; list_repos() returns empty afterward."""
    path = repo_factory.make_clean("todelete")

    record = await workspace_service.register_repo(str(path))
    await workspace_service.unregister_repo(record.id)

    repos = await workspace_service.list_repos()
    ids = [r.id for r in repos]
    assert record.id not in ids


# ---------------------------------------------------------------------------
# Worktree tests (Task 3.2)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def worktree_service(
    db: DatabaseManager, event_service: EventService, workspace_service
):
    from fleet.workspace.service import WorktreeService

    return WorktreeService(db, event_service, workspace_service)


def _insert_agent(db: DatabaseManager, agent_id: str) -> None:
    """Insert a minimal agent row to satisfy worktrees FK constraint.

    Uses agent_id as both id and name to ensure UNIQUE(scope, name) is
    satisfied per agent across tests.
    """
    from datetime import UTC, datetime

    from sqlalchemy import text as sql_text

    now = datetime.now(UTC).isoformat()
    cols = (
        "id, name, scope, role, backend, model, status,"
        " created_at, updated_at"
    )
    vals = (
        ":id, :name, :scope, :role, :backend, :model, :status,"
        " :created_at, :updated_at"
    )
    with db.read_connection() as conn:
        conn.execute(
            sql_text(f"INSERT INTO agents ({cols}) VALUES ({vals})"),
            {
                "id": agent_id,
                "name": agent_id,  # unique per call since agent_id differs
                "scope": "test",
                "role": "worker",
                "backend": "anthropic",
                "model": "claude-3-5-sonnet-20241022",
                "status": "idle",
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.commit()


@pytest.mark.asyncio
async def test_create_worktree_creates_branch(
    worktree_service, workspace_service, repo_factory, db: DatabaseManager
) -> None:
    """Worktree dir exists and branch matches fleet/<task_id>-<name>."""
    path = repo_factory.make_clean("wt-clean")
    repo = await workspace_service.register_repo(str(path))

    agent_id = "agent-wt-1"
    _insert_agent(db, agent_id)

    record = await worktree_service.create_worktree(
        repo_id=repo.id,
        agent_id=agent_id,
        task_id="task-001",
        name="my feature",
        owned_paths=["src/"],
    )

    import re
    expected_branch_pattern = re.compile(r"^fleet/task-001-my-feature$")
    assert expected_branch_pattern.match(record.branch), (
        f"branch {record.branch!r} should match fleet/<task_id>-<name>"
    )
    from pathlib import Path
    assert Path(record.path).exists(), "worktree directory should exist on disk"
    assert record.status == "active"
    assert record.repository_id == repo.id
    assert record.agent_id == agent_id


@pytest.mark.asyncio
async def test_create_worktree_dirty_repo_raises(
    worktree_service, workspace_service, repo_factory, db: DatabaseManager
) -> None:
    """create_worktree on a dirty repo raises DirtyRepoError with options list."""
    from fleet.workspace.service import DirtyRepoError

    path = repo_factory.make_dirty("wt-dirty")
    repo = await workspace_service.register_repo(str(path))

    agent_id = "agent-wt-2"
    _insert_agent(db, agent_id)

    with pytest.raises(DirtyRepoError) as exc_info:
        await worktree_service.create_worktree(
            repo_id=repo.id,
            agent_id=agent_id,
            task_id="task-002",
            name="dirty-branch",
            owned_paths=[],
        )

    err = exc_info.value
    assert hasattr(err, "options"), "DirtyRepoError must have .options attribute"
    assert "continue_dirty" in err.options
    assert "stash" in err.options
    assert "commit" in err.options
    assert "cancel" in err.options


@pytest.mark.asyncio
async def test_create_worktree_overlap_raises(
    worktree_service, workspace_service, repo_factory, db: DatabaseManager
) -> None:
    """Two worktrees claiming the same owned_paths raises OverlapError."""
    from fleet.workspace.service import OverlapError

    path = repo_factory.make_clean("wt-overlap")
    repo = await workspace_service.register_repo(str(path))

    agent_id_1 = "agent-wt-3a"
    agent_id_2 = "agent-wt-3b"
    _insert_agent(db, agent_id_1)
    _insert_agent(db, agent_id_2)

    # First worktree claims "src/*" (a glob pattern)
    await worktree_service.create_worktree(
        repo_id=repo.id,
        agent_id=agent_id_1,
        task_id="task-003a",
        name="feat-a",
        owned_paths=["src/*"],
    )

    # Second worktree claims a concrete path that matches the glob — should overlap
    with pytest.raises(OverlapError) as exc_info:
        await worktree_service.create_worktree(
            repo_id=repo.id,
            agent_id=agent_id_2,
            task_id="task-003b",
            name="feat-b",
            owned_paths=["src/main.py"],
        )

    err = exc_info.value
    assert hasattr(err, "conflicting_worktree_id"), (
        "OverlapError must carry .conflicting_worktree_id"
    )


@pytest.mark.asyncio
async def test_remove_worktree_cleans_up(
    worktree_service, workspace_service, repo_factory, db: DatabaseManager
) -> None:
    """remove_worktree: worktree dir gone, status=removed."""
    from pathlib import Path

    path = repo_factory.make_clean("wt-remove")
    repo = await workspace_service.register_repo(str(path))

    agent_id = "agent-wt-4"
    _insert_agent(db, agent_id)

    record = await worktree_service.create_worktree(
        repo_id=repo.id,
        agent_id=agent_id,
        task_id="task-004",
        name="to-remove",
        owned_paths=[],
    )

    worktree_path = Path(record.path)
    assert worktree_path.exists(), "worktree should exist before removal"

    await worktree_service.remove_worktree(record.id)

    assert not worktree_path.exists(), "worktree directory should be gone after removal"

    # Status should be updated in DB.
    worktrees = await worktree_service.list_worktrees(repo.id)
    removed = [w for w in worktrees if w.id == record.id]
    assert len(removed) == 1
    assert removed[0].status == "removed"


@pytest.mark.asyncio
async def test_wip_report_returns_ahead_count(
    worktree_service, workspace_service, repo_factory, db: DatabaseManager
) -> None:
    """get_wip_report returns dict with ahead, dirty_files, branch keys."""
    path = repo_factory.make_clean("wt-wip")
    repo = await workspace_service.register_repo(str(path))

    agent_id = "agent-wt-5"
    _insert_agent(db, agent_id)

    record = await worktree_service.create_worktree(
        repo_id=repo.id,
        agent_id=agent_id,
        task_id="task-005",
        name="wip-branch",
        owned_paths=[],
    )

    report = await worktree_service.get_wip_report(record.id)

    assert "ahead" in report, "wip report must contain 'ahead'"
    assert "dirty_files" in report, "wip report must contain 'dirty_files'"
    assert "branch" in report, "wip report must contain 'branch'"
    assert isinstance(report["ahead"], int), "'ahead' must be an int"
    assert isinstance(report["dirty_files"], list), "'dirty_files' must be a list"
    assert isinstance(report["branch"], str), "'branch' must be a str"
    assert report["ahead"] >= 0


@pytest.mark.asyncio
async def test_create_worktree_emits_git_action_event(
    worktree_service, workspace_service, repo_factory, db: DatabaseManager,
    event_service: EventService,
) -> None:
    """After create_worktree(), events table has a git_action event."""
    path = repo_factory.make_clean("wt-event")
    repo = await workspace_service.register_repo(str(path))

    agent_id = "agent-wt-6"
    _insert_agent(db, agent_id)

    await worktree_service.create_worktree(
        repo_id=repo.id,
        agent_id=agent_id,
        task_id="task-006",
        name="event-branch",
        owned_paths=[],
    )

    events = await event_service.query("workspace", type_filter="git_action")
    assert len(events) >= 1, "at least one git_action event should be emitted"
    payloads = [e.payload for e in events]
    actions = [p.get("action") for p in payloads]
    assert "worktree_created" in actions, (
        "git_action event should have action=worktree_created"
    )
