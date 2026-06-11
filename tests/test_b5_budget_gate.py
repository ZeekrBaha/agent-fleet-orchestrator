"""B5: Pre-turn budget gate + per-scope aggregate cap.

TDD RED phase — all tests must fail before the fix.

Behaviors tested:
1. check_pre_turn() returns PAUSE when agent is already at hard limit.
2. check_pre_turn() returns OK when agent is under limit.
3. check_pre_turn() returns PAUSE when scope aggregate exceeds scope cap.
4. AgentSession._run_turn skips backend.send() when check_pre_turn() returns PAUSE.
5. Settings exposes scope_budget_hard_usd field.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import Connection, text

from fleet.agents.budget import BudgetAction, BudgetEnforcer
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService, create_event_service
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "b5_budget.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager) -> EventService:
    return create_event_service(db, SSEHub())


async def _insert_agent(
    db: DatabaseManager,
    agent_id: str,
    scope: str,
    *,
    cost_usd: float = 0.0,
    budget_hard_usd: float | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()

    def _write(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  cost_usd, budget_hard_usd, created_at, updated_at)"
                " VALUES"
                " (:id, :name, :scope, :role, :backend, :model, :status,"
                "  :cost_usd, :budget_hard_usd, :now, :now)"
            ),
            {
                "id": agent_id, "name": agent_id, "scope": scope,
                "role": "worker", "backend": "mock", "model": "test",
                "status": "idle", "cost_usd": cost_usd,
                "budget_hard_usd": budget_hard_usd, "now": now,
            },
        )
        conn.commit()

    await db.write(_write)


# ---------------------------------------------------------------------------
# 1. check_pre_turn returns PAUSE when agent is at hard cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_pre_turn_pause_when_at_hard_cap(
    db: DatabaseManager, event_svc: EventService
) -> None:
    """check_pre_turn() must return PAUSE when cost_usd >= budget_hard_usd.

    Fails before B5: BudgetEnforcer has no check_pre_turn() method.
    """
    await _insert_agent(
        db, "agent-at-cap", "scope-a",
        cost_usd=1.0, budget_hard_usd=1.0,
    )
    enforcer = BudgetEnforcer(
        agent_id="agent-at-cap",
        scope="scope-a",
        db=db,
        event_service=event_svc,
    )
    action = await enforcer.check_pre_turn()
    assert action == BudgetAction.PAUSE, (
        f"Expected PAUSE when cost=1.0 >= hard=1.0, got {action}"
    )


@pytest.mark.asyncio
async def test_check_pre_turn_ok_when_under_cap(
    db: DatabaseManager, event_svc: EventService
) -> None:
    """check_pre_turn() must return OK when cost_usd < budget_hard_usd."""
    await _insert_agent(
        db, "agent-under-cap", "scope-a",
        cost_usd=0.5, budget_hard_usd=1.0,
    )
    enforcer = BudgetEnforcer(
        agent_id="agent-under-cap",
        scope="scope-a",
        db=db,
        event_service=event_svc,
    )
    action = await enforcer.check_pre_turn()
    assert action == BudgetAction.OK, (
        f"Expected OK when cost=0.5 < hard=1.0, got {action}"
    )


# ---------------------------------------------------------------------------
# 2. check_pre_turn respects per-scope aggregate cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_pre_turn_pause_on_scope_cap(
    db: DatabaseManager, event_svc: EventService
) -> None:
    """check_pre_turn() returns PAUSE when scope aggregate >= scope_budget_hard_usd.

    The calling agent may be under its own limit but blocked by the scope cap.
    Fails before B5: BudgetEnforcer has no scope_budget_hard_usd parameter.
    """
    scope = "scope-scope-cap"
    # Agent A used 0.8, agent B (the caller) used 0.3 → scope total = 1.1
    await _insert_agent(db, "agent-a", scope, cost_usd=0.8, budget_hard_usd=2.0)
    await _insert_agent(db, "agent-b", scope, cost_usd=0.3, budget_hard_usd=2.0)

    enforcer = BudgetEnforcer(
        agent_id="agent-b",
        scope=scope,
        db=db,
        event_service=event_svc,
        scope_budget_hard_usd=1.0,  # scope cap = $1.00
    )
    action = await enforcer.check_pre_turn()
    assert action == BudgetAction.PAUSE, (
        f"Scope total 1.1 >= scope_cap 1.0 → expected PAUSE, got {action}"
    )


@pytest.mark.asyncio
async def test_check_pre_turn_ok_when_scope_under_cap(
    db: DatabaseManager, event_svc: EventService
) -> None:
    """check_pre_turn() returns OK when scope aggregate < scope_budget_hard_usd."""
    scope = "scope-scope-ok"
    await _insert_agent(db, "agent-c", scope, cost_usd=0.3, budget_hard_usd=2.0)
    await _insert_agent(db, "agent-d", scope, cost_usd=0.2, budget_hard_usd=2.0)

    enforcer = BudgetEnforcer(
        agent_id="agent-d",
        scope=scope,
        db=db,
        event_service=event_svc,
        scope_budget_hard_usd=1.0,
    )
    action = await enforcer.check_pre_turn()
    assert action == BudgetAction.OK


# ---------------------------------------------------------------------------
# 3. AgentSession skips backend.send when check_pre_turn returns PAUSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_skips_backend_when_pre_turn_paused(
    db: DatabaseManager, event_svc: EventService, tmp_path
) -> None:
    """When agent is at hard cap, _run_turn must NOT call backend.send().

    Fails before B5: session always calls backend.send before checking budget.
    """
    from unittest.mock import AsyncMock, MagicMock

    from fleet.agents.inbox import InboxService
    from fleet.agents.session import AgentSession

    scope = "scope-gate"
    agent_id = "agent-gate"
    now = datetime.now(UTC).isoformat()

    # Insert agent already at hard cap (cost == budget_hard)
    def _setup(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  cost_usd, budget_hard_usd, created_at, updated_at)"
                " VALUES (:id, :n, :sc, 'worker', 'mock', 'test',"
                "  'idle', 1.0, 1.0, :now, :now)"
            ),
            {"id": agent_id, "n": agent_id, "sc": scope, "now": now},
        )
        conn.commit()

    await db.write(_setup)

    inbox = InboxService(db)

    # Mock backend: send should NOT be called
    backend = MagicMock()
    backend.send = AsyncMock()
    backend.events = MagicMock(return_value=iter([]))
    backend.start = AsyncMock(return_value="session-ref")
    backend.interrupt = AsyncMock()

    enforcer = BudgetEnforcer(
        agent_id=agent_id,
        scope=scope,
        db=db,
        event_service=event_svc,
    )

    session = AgentSession(
        agent_id=agent_id,
        scope=scope,
        backend=backend,
        event_service=event_svc,
        inbox=inbox,
        db=db,
        budget=enforcer,
    )
    session._session_ref = "session-ref"

    # Place an inbox message
    await inbox.enqueue(agent_id, "test-sender", "do some work")
    msg = await inbox.deliver_next(agent_id)
    assert msg is not None
    assert msg.id is not None

    # Run the turn: pre-turn gate must block and pause WITHOUT calling send
    # We run with a short timeout since the session will try to wait for resume
    try:
        await asyncio.wait_for(
            session._run_turn(msg.message, msg.id),
            timeout=0.5,
        )
    except TimeoutError:
        pass  # Expected: session blocked waiting for budget approval

    # Key assertion: backend.send was NOT called
    backend.send.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Settings exposes scope_budget_hard_usd
# ---------------------------------------------------------------------------


def test_settings_has_scope_budget_hard_usd() -> None:
    """Settings must have a scope_budget_hard_usd field (default None).

    Fails before B5: field doesn't exist in config.py.
    """
    from fleet.config import Settings

    s = Settings()
    assert hasattr(s, "scope_budget_hard_usd"), (
        "Settings missing scope_budget_hard_usd field"
    )
    assert s.scope_budget_hard_usd is None, (
        "Default should be None (no scope cap)"
    )
