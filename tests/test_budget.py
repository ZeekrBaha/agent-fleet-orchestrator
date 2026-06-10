"""Tests for BudgetEnforcer (Task 2.4).

TDD: written BEFORE the implementation in fleet/agents/budget.py.
All tests should FAIL before budget.py is created.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import Connection, text

from fleet.agents.backends.protocol import TurnEnd
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService, create_event_service
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_budget.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def hub() -> SSEHub:
    return SSEHub()


@pytest_asyncio.fixture
async def event_service(db: DatabaseManager, hub: SSEHub) -> EventService:
    return create_event_service(db, hub)


async def _insert_agent(
    db: DatabaseManager,
    agent_id: str,
    scope: str,
    *,
    budget_soft_usd: float | None = None,
    budget_hard_usd: float | None = None,
) -> None:
    """Helper: insert a minimal agent row for testing."""
    now = datetime.now(UTC).isoformat()

    def _write(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  budget_soft_usd, budget_hard_usd, created_at, updated_at)"
                " VALUES"
                " (:id, :name, :scope, :role, :backend, :model, 'idle',"
                "  :budget_soft_usd, :budget_hard_usd, :created_at, :updated_at)"
            ),
            {
                "id": agent_id,
                "name": "test-agent",
                "scope": scope,
                "role": "worker",
                "backend": "mock",
                "model": "mock",
                "budget_soft_usd": budget_soft_usd,
                "budget_hard_usd": budget_hard_usd,
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.commit()

    await db.write(_write)


def _make_turn_end(cost_usd: float) -> TurnEnd:
    return TurnEnd(
        cost_usd=cost_usd,
        input_tokens=10,
        output_tokens=10,
        context_pct=0.1,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_limits_returns_ok(
    db: DatabaseManager, event_service: EventService
) -> None:
    """Agent with no budget limits → BudgetAction.OK regardless of cost."""
    from fleet.agents.budget import BudgetAction, BudgetEnforcer

    agent_id = "agent-no-limits"
    scope = "scope-no-limits"
    await _insert_agent(db, agent_id, scope, budget_soft_usd=None, budget_hard_usd=None)

    enforcer = BudgetEnforcer(agent_id, scope, db, event_service)
    action = await enforcer.record_turn_cost(_make_turn_end(cost_usd=999.0))

    assert action == BudgetAction.OK


@pytest.mark.asyncio
async def test_below_soft_returns_ok(
    db: DatabaseManager, event_service: EventService
) -> None:
    """Cost below soft limit → BudgetAction.OK, no alert event emitted."""
    from fleet.agents.budget import BudgetAction, BudgetEnforcer

    agent_id = "agent-below-soft"
    scope = "scope-below-soft"
    await _insert_agent(db, agent_id, scope, budget_soft_usd=1.0, budget_hard_usd=2.0)

    enforcer = BudgetEnforcer(agent_id, scope, db, event_service)
    action = await enforcer.record_turn_cost(_make_turn_end(cost_usd=0.5))

    assert action == BudgetAction.OK

    # No budget_alert event should have been emitted
    events = await event_service.query(scope, type_filter="budget_alert")
    assert len(events) == 0


@pytest.mark.asyncio
async def test_at_soft_returns_warn(
    db: DatabaseManager, event_service: EventService
) -> None:
    """Cost accumulates to exactly soft limit → WARN + budget_alert level=soft."""
    from fleet.agents.budget import BudgetAction, BudgetEnforcer

    agent_id = "agent-at-soft"
    scope = "scope-at-soft"
    await _insert_agent(db, agent_id, scope, budget_soft_usd=1.0, budget_hard_usd=2.0)

    enforcer = BudgetEnforcer(agent_id, scope, db, event_service)
    # Single turn that lands exactly on the soft limit
    action = await enforcer.record_turn_cost(_make_turn_end(cost_usd=1.0))

    assert action == BudgetAction.WARN

    # A budget_alert event with level=soft must exist
    events = await event_service.query(scope, type_filter="budget_alert")
    assert len(events) == 1
    assert events[0].payload["level"] == "soft"
    assert events[0].payload["cost_usd"] == pytest.approx(1.0)
    assert events[0].payload["limit_usd"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_at_hard_returns_pause(
    db: DatabaseManager, event_service: EventService
) -> None:
    """Cost accumulates to hard limit → BudgetAction.PAUSE + budget_alert level=hard."""
    from fleet.agents.budget import BudgetAction, BudgetEnforcer

    agent_id = "agent-at-hard"
    scope = "scope-at-hard"
    await _insert_agent(db, agent_id, scope, budget_soft_usd=1.0, budget_hard_usd=2.0)

    enforcer = BudgetEnforcer(agent_id, scope, db, event_service)
    action = await enforcer.record_turn_cost(_make_turn_end(cost_usd=2.0))

    assert action == BudgetAction.PAUSE

    events = await event_service.query(scope, type_filter="budget_alert")
    assert len(events) == 1
    assert events[0].payload["level"] == "hard"
    assert events[0].payload["cost_usd"] == pytest.approx(2.0)
    assert events[0].payload["limit_usd"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_cumulative_cost_accumulates(
    db: DatabaseManager, event_service: EventService
) -> None:
    """Two turns of 0.5 each; soft=0.8, hard=2.0 → first OK, second WARN."""
    from fleet.agents.budget import BudgetAction, BudgetEnforcer

    agent_id = "agent-cumulative"
    scope = "scope-cumulative"
    await _insert_agent(db, agent_id, scope, budget_soft_usd=0.8, budget_hard_usd=2.0)

    enforcer = BudgetEnforcer(agent_id, scope, db, event_service)

    first = await enforcer.record_turn_cost(_make_turn_end(cost_usd=0.5))
    assert first == BudgetAction.OK

    second = await enforcer.record_turn_cost(_make_turn_end(cost_usd=0.5))
    assert second == BudgetAction.WARN

    # Cumulative cost in DB should be 1.0
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT cost_usd FROM agents WHERE id = :id"),
            {"id": agent_id},
        ).fetchone()
    assert row is not None
    assert row.cost_usd == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_hard_limit_creates_approval_row(
    db: DatabaseManager, event_service: EventService
) -> None:
    """Hard limit hit → pending approval row with operation=over_budget_continue."""
    from fleet.agents.budget import BudgetAction, BudgetEnforcer

    agent_id = "agent-hard-approval"
    scope = "scope-hard-approval"
    await _insert_agent(db, agent_id, scope, budget_soft_usd=1.0, budget_hard_usd=2.0)

    enforcer = BudgetEnforcer(agent_id, scope, db, event_service)
    action = await enforcer.record_turn_cost(_make_turn_end(cost_usd=2.0))

    assert action == BudgetAction.PAUSE

    with db.read_connection() as conn:
        rows = conn.execute(
            text(
                "SELECT id, scope, requester_agent_id, operation, status"
                " FROM approvals WHERE requester_agent_id = :agent_id"
            ),
            {"agent_id": agent_id},
        ).fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row.scope == scope
    assert row.requester_agent_id == agent_id
    assert row.operation == "over_budget_continue"
    assert row.status == "pending"
