"""C2: Hibernate state transition — emit 'waiting' only once.

Before fix: session.py emits state_change('waiting') on every idle timeout loop
iteration, spamming the events table every idle_hibernate_s seconds indefinitely.

After fix: 'waiting' is emitted only on the first transition (when status
transitions from non-waiting to waiting). Subsequent timeout cycles skip the
emit since status is already 'waiting'.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text

from fleet.agents.budget import BudgetEnforcer
from fleet.agents.inbox import InboxService
from fleet.agents.session import AgentSession
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "hibernate.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager) -> EventService:
    return EventService(db, SSEHub())


def _insert_agent(db: DatabaseManager, agent_id: str, scope: str) -> None:
    from sqlalchemy.orm import Connection

    now = datetime.now(UTC).isoformat()

    def _write(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  context_pct, cost_usd, created_at, updated_at)"
                " VALUES (:id, :id, :sc, 'worker', 'mock', 'test',"
                "  'idle', 0, 0, :now, :now)"
            ),
            {"id": agent_id, "sc": scope, "now": now},
        )
        conn.commit()

    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(db.write(_write))


@pytest.mark.asyncio
async def test_waiting_emitted_once_on_repeated_idle_timeouts(
    db: DatabaseManager,
    event_svc: EventService,
) -> None:
    """Idle timeout fires 3+ times — 'waiting' emitted exactly once (first transition).

    Fails before fix: repeated TimeoutError → repeated _set_status('waiting')
    → N events instead of 1.
    """
    agent_id = "agent-hibernate"
    scope = "scope-h"
    now = datetime.now(UTC).isoformat()

    def _setup(conn: Any) -> None:
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  context_pct, cost_usd, created_at, updated_at)"
                " VALUES (:id, :id, :sc, 'worker', 'mock', 'test',"
                "  'idle', 0, 0, :now, :now)"
            ),
            {"id": agent_id, "sc": scope, "now": now},
        )
        conn.commit()

    await db.write(_setup)

    inbox = InboxService(db)

    backend = MagicMock()
    backend.start = AsyncMock(return_value="ref")
    backend.interrupt = AsyncMock()
    # No events — the session should just idle
    backend.send = AsyncMock()
    backend.events = MagicMock(return_value=iter([]))

    budget = BudgetEnforcer(
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
        budget=budget,
        idle_hibernate_s=0.05,  # 50ms — fast enough to trigger 4+ cycles in 0.25s
    )

    # Run session for 0.25s — enough for 4-5 timeout cycles
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.25)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # Count 'waiting' state_change events
    with db.read_connection() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM events"
                " WHERE type = 'state_change' AND summary LIKE '% → waiting'"
            )
        ).fetchone()

    waiting_count = row[0] if row else 0
    assert waiting_count == 1, (
        f"Expected exactly 1 'waiting' state_change (first transition only), "
        f"got {waiting_count} — session is spamming 'waiting' on every timeout"
    )
