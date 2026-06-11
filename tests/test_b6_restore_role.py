"""B6: Restored sessions preserve role; wait_for_decision checks DB on restart.

TDD RED phase — all tests must fail before the fix.

Behaviors tested:
1. restore_sessions preserves orchestrator role (not demoted to worker).
2. restore_sessions preserves coder role (sanity check with a different role).
3. wait_for_decision returns immediately when decision already in DB (restart scenario).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import Connection, text

from fleet.agents.inbox import InboxService
from fleet.agents.service import AgentService
from fleet.approvals.service import ApprovalService
from fleet.db import DatabaseManager, init_db
from fleet.events.service import create_event_service
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path) -> DatabaseManager:
    manager = await init_db(str(tmp_path / "b6.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager):
    return create_event_service(db, SSEHub())


@pytest_asyncio.fixture
async def agent_svc(db: DatabaseManager, event_svc):
    svc = AgentService(db=db, event_service=event_svc, inbox_service=InboxService(db))
    yield svc
    await svc.stop_all()


def _insert_agent(
    conn: Connection,
    agent_id: str,
    scope: str,
    role: str,
    now: str,
) -> None:
    conn.execute(
        text(
            "INSERT INTO agents"
            " (id, name, scope, role, backend, model, status,"
            "  cost_usd, created_at, updated_at)"
            " VALUES (:id, :n, :sc, :role, 'mock', 'test', 'idle',"
            "  0.0, :now, :now)"
        ),
        {"id": agent_id, "n": agent_id, "sc": scope, "role": role, "now": now},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. restore_sessions preserves orchestrator role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_keeps_orchestrator_role(
    db: DatabaseManager, agent_svc: AgentService
) -> None:
    """Restored orchestrator must NOT be demoted to worker.

    Fails before B6: restore_sessions calls _start_session without role,
    defaulting to 'worker', so session._role == 'worker' not 'orchestrator'.
    """
    now = datetime.now(UTC).isoformat()

    def _setup(conn: Connection) -> None:
        _insert_agent(conn, "orch-1", "scope-b6", "orchestrator", now)

    await db.write(_setup)
    await agent_svc.restore_sessions()

    session = agent_svc._sessions.get("orch-1")
    assert session is not None, "Session not restored"
    assert session._role == "orchestrator", (
        f"Expected role='orchestrator', got {session._role!r}"
    )


# ---------------------------------------------------------------------------
# 2. restore_sessions preserves coder role (sanity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_keeps_coder_role(
    db: DatabaseManager, agent_svc: AgentService
) -> None:
    """Restored coder stays coder, not demoted to worker."""
    now = datetime.now(UTC).isoformat()

    def _setup(conn: Connection) -> None:
        _insert_agent(conn, "coder-1", "scope-b6", "coder", now)

    await db.write(_setup)
    await agent_svc.restore_sessions()

    session = agent_svc._sessions.get("coder-1")
    assert session is not None
    assert session._role == "coder", (
        f"Expected role='coder', got {session._role!r}"
    )


# ---------------------------------------------------------------------------
# 3. wait_for_decision returns immediately when decision pre-exists in DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_decision_returns_immediately_when_already_decided(
    db: DatabaseManager, event_svc
) -> None:
    """After restart, if approval was decided while down, waiter returns instantly.

    Fails before B6: wait_for_decision creates fresh event and awaits it —
    it will never fire for a pre-existing DB decision, hanging until timeout.
    """
    approval_svc = ApprovalService(db=db, event_service=event_svc)
    scope = "scope-b6"
    now = datetime.now(UTC).isoformat()

    approval_id = "approval-already-decided"

    def _setup(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO approvals"
                " (id, scope, requester_agent_id, operation, rationale,"
                "  risk, status, decided_by, comment, created_at, decided_at)"
                " VALUES (:id, :sc, 'agent-x', 'budget_exceeded', 'test',"
                "  'low', 'approved', 'human', '', :now, :now)"
            ),
            {"id": approval_id, "sc": scope, "now": now},
        )
        conn.commit()

    await db.write(_setup)

    # No in-memory waiter — simulates fresh process restart
    assert approval_id not in approval_svc._waiters

    # Must return immediately, not block for timeout_s seconds
    result = await asyncio.wait_for(
        approval_svc.wait_for_decision(approval_id),
        timeout=1.0,
    )
    assert result == "approve", f"Expected 'approve', got {result!r}"
