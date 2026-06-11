"""P1-2: Budget check+reserve is atomic (TOCTOU fix).

Without atomicity, two concurrent check_pre_turn calls can both read the same
balance, both pass the cap check, and both proceed — overshooting the limit.

Fix: check_pre_turn runs inside db.write() and reserves remaining budget as
part of the same write.  The single-writer queue serializes it, so the second
concurrent call sees the reservation and returns PAUSE.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from fleet.agents.budget import BudgetAction, BudgetEnforcer
from fleet.agents.backends.protocol import TurnEnd
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _insert_agent(
    db_path: str,
    *,
    agent_id: str,
    cost_usd: float = 0.0,
    budget_hard_usd: float | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO agents"
        " (id, name, scope, role, backend, model, status,"
        "  context_pct, cost_usd, budget_hard_usd, created_at, updated_at)"
        " VALUES (?, 'a', 'test', 'worker', 'mock', 'mock',"
        "  'idle', 0, ?, ?, ?, ?)",
        (agent_id, cost_usd, budget_hard_usd, _now(), _now()),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "budget_atomic.db")


@pytest_asyncio.fixture
async def db(db_path: str) -> Any:
    manager = await init_db(db_path)
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager) -> EventService:
    return EventService(db, SSEHub())


def _make_enforcer(
    db: DatabaseManager, event_svc: EventService, agent_id: str
) -> BudgetEnforcer:
    return BudgetEnforcer(
        agent_id=agent_id, scope="test", db=db, event_service=event_svc
    )


@pytest.mark.asyncio
async def test_concurrent_turns_cannot_exceed_cap(
    db: DatabaseManager, event_svc: EventService, db_path: str
) -> None:
    """Two concurrent check_pre_turn calls with budget for only one turn.

    Exactly one must return OK; the other must return PAUSE.
    Final cost_usd must not exceed budget_hard_usd after both turns complete.

    RED: fails before atomic fix because both reads see the same (under-cap)
    balance and both return OK.
    """
    agent_id = "budget-race-agent"
    budget = 1.0
    _insert_agent(db_path, agent_id=agent_id, cost_usd=0.0, budget_hard_usd=budget)

    enforcer_a = _make_enforcer(db, event_svc, agent_id)
    enforcer_b = _make_enforcer(db, event_svc, agent_id)

    results = await asyncio.gather(
        enforcer_a.check_pre_turn(),
        enforcer_b.check_pre_turn(),
    )

    ok_count = sum(1 for r in results if r == BudgetAction.OK)
    pause_count = sum(1 for r in results if r == BudgetAction.PAUSE)

    assert ok_count == 1, (
        f"Exactly one turn should proceed, got {ok_count} OK results: {results}"
    )
    assert pause_count == 1, (
        f"Exactly one turn should be paused, got {pause_count} PAUSE results: {results}"
    )


@pytest.mark.asyncio
async def test_reservation_refunded_on_cheap_turn(
    db: DatabaseManager, event_svc: EventService, db_path: str
) -> None:
    """When actual turn cost < reservation, the balance reflects actual cost only."""
    agent_id = "budget-reconcile-agent"
    budget = 1.0
    actual_cost = 0.30
    _insert_agent(db_path, agent_id=agent_id, cost_usd=0.0, budget_hard_usd=budget)

    enforcer = _make_enforcer(db, event_svc, agent_id)
    action = await enforcer.check_pre_turn()
    assert action == BudgetAction.OK, f"Expected OK, got {action}"

    # Simulate turn completion with cost less than the full reservation.
    turn_end = TurnEnd(
        cost_usd=actual_cost, input_tokens=100, output_tokens=50, context_pct=0.5
    )
    await enforcer.record_turn_cost(turn_end)

    from sqlalchemy import text
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT cost_usd FROM agents WHERE id = :id"),
            {"id": agent_id},
        ).fetchone()

    assert row is not None
    # Final cost should equal actual_cost, not the full reservation (budget).
    assert abs(row[0] - actual_cost) < 0.001, (
        f"Expected cost_usd ≈ {actual_cost}, got {row[0]}"
    )
