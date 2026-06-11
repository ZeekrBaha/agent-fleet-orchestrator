"""Tests for T4: backend factory + spawn wiring fixes.

TDD: written BEFORE the implementation. Tests should FAIL before fixes.

Bugs covered:
    P1-3 — _make_backend must support "claude" (currently only "mock")
    P1-4 — spawn event must include action="spawn" in payload
    P1-5 — task_description must be forwarded to create_agent
    P1-6 — worktree_id must be written back to agent after spawn
    (+)   — AgentService.set_worktree_id must exist and update the DB row
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from fleet.api.agents import _make_backend
from fleet.api.tool_handlers import _handle_spawn_worker
from fleet.api.tool_schemas import SpawnWorkerInput
from fleet.db import DatabaseManager, init_db

# ---------------------------------------------------------------------------
# P1-3: _make_backend factory supports "claude"
# ---------------------------------------------------------------------------


def test_make_backend_claude_returns_claude_backend() -> None:
    """_make_backend('claude') must return a ClaudeBackend instance."""
    from fleet.agents.backends.claude import ClaudeBackend

    result = _make_backend("claude")  # type: ignore[arg-type]
    assert isinstance(result, ClaudeBackend)


def test_make_backend_mock_returns_mock_backend() -> None:
    """_make_backend('mock') still returns a MockBackend (regression guard)."""
    from fleet.agents.backends.mock import MockBackend

    result = _make_backend("mock")
    assert isinstance(result, MockBackend)


def test_make_backend_invalid_raises_value_error() -> None:
    """Unknown backend type must raise ValueError with a descriptive message."""
    with pytest.raises(ValueError, match="Unknown backend type"):
        _make_backend("bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shared helpers for _handle_spawn_worker tests
# ---------------------------------------------------------------------------


def _make_mock_svcs(*, worktree_id: str | None = None) -> dict[str, Any]:
    """Return a minimal svcs dict suitable for calling _handle_spawn_worker."""
    mock_record = MagicMock()
    mock_record.id = "new-agent-uuid"
    mock_record.name = "worker-1"
    mock_record.status = "idle"

    agent_svc = AsyncMock()
    agent_svc.create_agent.return_value = mock_record
    agent_svc.list_agents.return_value = []  # no live workers (rate check)

    event_svc = AsyncMock()
    event_svc.query.return_value = []  # no recent spawns (rate check)

    policy_svc = MagicMock()
    policy_svc.check_spawn_rate = MagicMock()  # no-op; never raises

    calling_agent = MagicMock()
    calling_agent.role = "orchestrator"  # unrestricted role

    svcs: dict[str, Any] = {
        "agent_svc": agent_svc,
        "event_svc": event_svc,
        "_policy_svc": policy_svc,
        "_calling_agent": calling_agent,
    }

    if worktree_id is not None:
        mock_worktree = MagicMock()
        mock_worktree.id = worktree_id
        worktree_svc = AsyncMock()
        worktree_svc.create_worktree.return_value = mock_worktree
        svcs["worktree_svc"] = worktree_svc

    return svcs


def _make_spawn_input(**overrides: Any) -> SpawnWorkerInput:
    defaults: dict[str, Any] = {
        "agent_id": "caller-agent-id",
        "scope": "test-scope",
        "name": "worker-1",
        "role": "worker",
        "task_description": "Fix the login bug in auth.py",
        "task_id": "task-001",
        "repository_id": "repo-abc",
    }
    defaults.update(overrides)
    return SpawnWorkerInput(**defaults)


# ---------------------------------------------------------------------------
# P1-4: spawn event payload must have action="spawn"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_event_has_action_spawn() -> None:
    """The task_created state_change event must carry action='spawn' in payload."""
    svcs = _make_mock_svcs()
    inp = _make_spawn_input()

    await _handle_spawn_worker(inp, svcs)

    event_svc: AsyncMock = svcs["event_svc"]
    state_change_calls = [
        c
        for c in event_svc.append.call_args_list
        if len(c.args) >= 2 and c.args[1] == "state_change"
    ]
    assert state_change_calls, "No state_change event was emitted"
    payloads = [c.kwargs.get("payload", {}) for c in state_change_calls]
    assert any(p.get("action") == "spawn" for p in payloads), (
        f"No state_change payload with action='spawn'. Got payloads: {payloads}"
    )


# ---------------------------------------------------------------------------
# P1-5: task_description must reach create_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_passes_task_description() -> None:
    """_handle_spawn_worker must forward task_description to agent_svc.create_agent."""
    svcs = _make_mock_svcs()
    task_desc = "Refactor the auth module to use JWT tokens"
    inp = _make_spawn_input(task_description=task_desc)

    await _handle_spawn_worker(inp, svcs)

    agent_svc: AsyncMock = svcs["agent_svc"]
    kwargs = agent_svc.create_agent.call_args.kwargs
    assert kwargs.get("task_description") == task_desc, (
        f"task_description was not forwarded to create_agent. Got kwargs: {kwargs}"
    )


# ---------------------------------------------------------------------------
# P1-6: worktree_id must be written back to agent after spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_sets_worktree_id_on_agent() -> None:
    """After create_worktree succeeds, set_worktree_id must be called on agent."""
    wt_id = "worktree-uuid-777"
    svcs = _make_mock_svcs(worktree_id=wt_id)
    inp = _make_spawn_input(task_id="task-001", repository_id="repo-abc")

    await _handle_spawn_worker(inp, svcs)

    agent_svc: AsyncMock = svcs["agent_svc"]
    # set_worktree_id must be awaited once with the agent id and the worktree id
    agent_svc.set_worktree_id.assert_awaited_once_with("new-agent-uuid", wt_id)


# ---------------------------------------------------------------------------
# AgentService.set_worktree_id — DB update
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_spawn_wiring.db"))
    yield manager
    await manager.close()


@pytest.mark.asyncio
async def test_agent_service_set_worktree_id(db: DatabaseManager) -> None:
    """AgentService.set_worktree_id updates worktree_id in the agents table."""
    from sqlalchemy import Connection, text

    from fleet.agents.service import AgentService
    from fleet.events.service import create_event_service
    from fleet.events.sse import SSEHub

    hub = SSEHub()
    event_svc = create_event_service(db, hub)
    inbox_svc = AsyncMock()

    agent_svc = AgentService(db, event_svc, inbox_svc)

    agent_id = "test-agent-id-42"
    worktree_id = "wt-set-test"
    now = "2024-01-01T00:00:00"

    # Insert a minimal agent row without going through create_agent
    def _insert(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  created_at, updated_at)"
                " VALUES (:id, 'test-agent', 'scope-1', 'worker', 'mock',"
                "  'claude-sonnet-4-6', 'idle', :now, :now)"
            ),
            {"id": agent_id, "now": now},
        )
        conn.commit()

    await db.write(_insert)

    # The method under test
    await agent_svc.set_worktree_id(agent_id, worktree_id)

    # Verify the DB was updated
    record = await agent_svc.get_agent(agent_id)
    assert record is not None
    assert record.worktree_id == worktree_id, (
        f"Expected worktree_id={worktree_id!r}, got {record.worktree_id!r}"
    )
