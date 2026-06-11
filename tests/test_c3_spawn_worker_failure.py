"""C3: spawn_worker worktree-creation failure returns degraded status.

Before fix: worktree creation failure is caught, audited, and silently
swallowed — response is identical to success. Caller cannot distinguish
"agent spawned with worktree" from "agent spawned but worktree failed".

After fix: response includes worktree_status field:
  "ok"       — worktree created successfully
  "degraded" — worktree creation failed (worktree_error key explains why)
  "skipped"  — no worktree_svc / task_id / repository_id (not requested)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub
from fleet.workspace.worktree_service import WorktreeError


@pytest_asyncio.fixture
async def db(tmp_path: Any):
    manager = await init_db(str(tmp_path / "c3.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager) -> EventService:
    return EventService(db, SSEHub())


def _make_svcs(
    db: DatabaseManager,
    event_svc: EventService,
    worktree_svc: Any = None,
) -> dict[str, Any]:
    """Build a minimal services dict for _handle_spawn_worker."""
    agent_svc = AsyncMock()
    agent_record = MagicMock()
    agent_record.id = "new-agent-1"
    agent_record.name = "worker-1"
    agent_record.status = "idle"
    agent_svc.create_agent = AsyncMock(return_value=agent_record)
    agent_svc.set_worktree_id = AsyncMock()
    agent_svc.get_agent = AsyncMock(return_value=None)
    agent_svc.list_agents = AsyncMock(return_value=[])

    policy_svc = MagicMock()
    policy_svc.check_spawn_rate = MagicMock()  # no-op
    policy_svc.check_secret_path = MagicMock()  # no-op

    calling_agent = MagicMock()
    calling_agent.role = "orchestrator"

    return {
        "agent_svc": agent_svc,
        "event_svc": event_svc,
        "workspace_svc": MagicMock(),
        "worktree_svc": worktree_svc,
        "db": db,
        "_policy_svc": policy_svc,
        "_calling_agent": calling_agent,
    }


def _spawn_input(with_worktree: bool = True) -> Any:
    from fleet.api.tool_schemas import SpawnWorkerInput

    return SpawnWorkerInput(
        agent_id="orchestrator-1",
        scope="scope-c3",
        name="worker-1",
        role="worker",
        backend_type="mock",
        model="test-model",
        task_id="task-1" if with_worktree else None,
        repository_id="repo-1" if with_worktree else None,
        owned_paths=[],
        task_description="test task",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_worktree_failure_returns_degraded_status(
    db: DatabaseManager,
    event_svc: EventService,
) -> None:
    """Worktree creation raises WorktreeError → response has worktree_status='degraded'.

    Fails before fix: response is {"agent_id": ..., "name": ..., "status": ...}
    with no worktree_status — caller cannot detect the failure.
    """
    from fleet.api.tool_handlers import _handle_spawn_worker

    failing_worktree_svc = AsyncMock()
    failing_worktree_svc.create_worktree = AsyncMock(
        side_effect=WorktreeError("git conflict")
    )

    svcs = _make_svcs(db, event_svc, worktree_svc=failing_worktree_svc)
    inp = _spawn_input(with_worktree=True)

    result = await _handle_spawn_worker(inp, svcs)

    assert result.get("worktree_status") == "degraded", (
        f"Expected worktree_status='degraded' on WorktreeError, got: {result}"
    )
    assert "worktree_error" in result, (
        f"Expected 'worktree_error' key in response, got: {result}"
    )


@pytest.mark.asyncio
async def test_spawn_worktree_success_returns_ok_status(
    db: DatabaseManager,
    event_svc: EventService,
) -> None:
    """Worktree creation succeeds → response has worktree_status='ok'."""
    from fleet.api.tool_handlers import _handle_spawn_worker

    worktree_record = MagicMock()
    worktree_record.id = "wt-1"

    succeeding_worktree_svc = AsyncMock()
    succeeding_worktree_svc.create_worktree = AsyncMock(return_value=worktree_record)

    svcs = _make_svcs(db, event_svc, worktree_svc=succeeding_worktree_svc)
    inp = _spawn_input(with_worktree=True)

    result = await _handle_spawn_worker(inp, svcs)

    assert result.get("worktree_status") == "ok", (
        f"Expected worktree_status='ok' on success, got: {result}"
    )


@pytest.mark.asyncio
async def test_spawn_without_worktree_returns_skipped_status(
    db: DatabaseManager,
    event_svc: EventService,
) -> None:
    """No task_id/repository_id → worktree_status='skipped' (not requested)."""
    from fleet.api.tool_handlers import _handle_spawn_worker

    svcs = _make_svcs(db, event_svc, worktree_svc=None)
    inp = _spawn_input(with_worktree=False)

    result = await _handle_spawn_worker(inp, svcs)

    assert result.get("worktree_status") == "skipped", (
        f"Expected worktree_status='skipped' when no worktree requested, got: {result}"
    )
