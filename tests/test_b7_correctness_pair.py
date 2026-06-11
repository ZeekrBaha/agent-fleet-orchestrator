"""B7: Relay client singleton + worktree base-branch drift fix.

TDD RED phase — all tests must fail before the fix.

Behaviors tested:
1. _get_relay() returns the same FleetRelay instance on repeated calls
   (no new httpx.AsyncClient per tool call).
2. create_worktree passes base_branch to worktree_add, not leaving it as HEAD.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import Connection, text

from fleet.db import DatabaseManager, init_db
from fleet.events.service import create_event_service
from fleet.events.sse import SSEHub
from fleet.workspace.service import RepositoryRecord
from fleet.workspace.worktree_service import WorktreeService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path) -> DatabaseManager:
    manager = await init_db(str(tmp_path / "b7.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager):
    return create_event_service(db, SSEHub())


# ---------------------------------------------------------------------------
# 1. _get_relay() is a singleton — same instance across calls
# ---------------------------------------------------------------------------


def test_get_relay_returns_singleton() -> None:
    """_get_relay() must return the same FleetRelay instance every call.

    Fails before B7: _get_relay() constructs a new FleetRelay (and a new
    httpx.AsyncClient) on every invocation.
    """
    import importlib
    import os

    # Patch env to avoid needing a real server
    with patch.dict(os.environ, {"FLEET_API_TOKEN": "test", "FLEET_BASE_URL": "http://127.0.0.1:9999"}):
        # Re-import fresh to get a clean module state
        import fleet.toolserver.main as mod
        importlib.reload(mod)

        relay_a = mod._get_relay()
        relay_b = mod._get_relay()
        relay_c = mod._get_relay()

    assert relay_a is relay_b, "Expected same relay instance on second call"
    assert relay_b is relay_c, "Expected same relay instance on third call"


# ---------------------------------------------------------------------------
# 2. create_worktree passes base_branch to worktree_add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_worktree_passes_base_branch_to_worktree_add(
    db: DatabaseManager, event_svc, tmp_path: Path
) -> None:
    """create_worktree must pass base_branch= to worktree_add.

    Fails before B7: worktree_add is called without base_branch, so the
    worktree is cut from HEAD even when HEAD != default_branch.
    """
    repo_path = str(tmp_path / "repo")
    Path(repo_path).mkdir()
    now = datetime.now(UTC).isoformat()
    repo_id = "repo-b7"
    default_branch = "main"

    def _setup(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO repositories (id, path, default_branch, created_at)"
                " VALUES (:id, :p, :db, :now)"
            ),
            {"id": repo_id, "p": repo_path, "db": default_branch, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  cost_usd, created_at, updated_at)"
                " VALUES ('agent-b7', 'agent-b7', 'scope-b7', 'worker',"
                "  'mock', 'test', 'idle', 0.0, :now, :now)"
            ),
            {"now": now},
        )
        conn.commit()

    await db.write(_setup)

    fake_repo = RepositoryRecord(
        id=repo_id,
        path=repo_path,
        default_branch=default_branch,
        merge_policy={},
        created_at=now,
    )
    workspace_mock = MagicMock()
    workspace_mock.get_repo = AsyncMock(return_value=fake_repo)

    svc = WorktreeService(
        db=db, event_service=event_svc, workspace_service=workspace_mock
    )

    captured_kwargs: dict = {}

    def _fake_worktree_add(repo_p, wt_path, branch, *, base_branch=None):
        captured_kwargs["base_branch"] = base_branch
        Path(wt_path).mkdir(parents=True, exist_ok=True)

    with (
        patch(
            "fleet.workspace.worktree_service.worktree_add",
            side_effect=_fake_worktree_add,
        ),
        patch("fleet.workspace.worktree_service.is_repo_dirty", return_value=False),
    ):
        await svc.create_worktree(
            repo_id=repo_id,
            agent_id="agent-b7",
            task_id="task-b7",
            name="fix-something",
            owned_paths=[],
        )

    assert captured_kwargs.get("base_branch") == default_branch, (
        f"Expected worktree_add called with base_branch={default_branch!r}, "
        f"got {captured_kwargs.get('base_branch')!r}"
    )
