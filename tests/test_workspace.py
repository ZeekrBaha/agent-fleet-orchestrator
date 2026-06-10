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
