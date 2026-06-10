"""Tests for AgentService lifecycle (Task 2.2).

TDD: these tests are written BEFORE the implementation exists.
All tests should FAIL before fleet/agents/service.py is created.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from fleet.agents.backends.mock import MockBackend
from fleet.agents.backends.protocol import (
    TextChunk,
    TurnEnd,
)
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def wait_for_status(
    agent_id: str, target_status: str, service, timeout: float = 3.0
):
    """Poll until agent reaches target_status or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        agent = await service.get_agent(agent_id)
        if agent and agent.status == target_status:
            return agent
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"Agent {agent_id!r} never reached status {target_status!r}"
    )


async def wait_for_event(
    scope: str,
    event_type: str,
    event_service: EventService,
    timeout: float = 3.0,
):
    """Poll until at least one event of the given type appears."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        events = await event_service.query(scope, type_filter=event_type)
        if events:
            return events
        await asyncio.sleep(0.05)
    raise TimeoutError(f"No {event_type!r} event appeared within {timeout}s")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_lifecycle.db"))
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
async def agent_service(db: DatabaseManager, event_service: EventService):
    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService

    inbox = InboxService(db)
    svc = AgentService(db, event_service, inbox)
    yield svc
    # Cancel all running session tasks on teardown
    for session in svc._sessions.values():
        task = session._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_returns_record(
    agent_service, db: DatabaseManager
) -> None:
    """create_agent() returns an AgentRecord with correct fields and status=idle."""
    backend = MockBackend(transcript=[])
    record = await agent_service.create_agent(
        scope="test-scope",
        name="test-agent",
        role="coder",
        backend=backend,
        model="mock-1",
    )

    assert record.id != ""
    assert record.name == "test-agent"
    assert record.role == "coder"
    assert record.model == "mock-1"
    assert record.status == "idle"
    assert record.scope == "test-scope"


@pytest.mark.asyncio
async def test_create_agent_emits_state_change_event(
    agent_service, event_service: EventService
) -> None:
    """create_agent() emits a state_change event into the events table."""
    backend = MockBackend(transcript=[])
    record = await agent_service.create_agent(
        scope="test-scope",
        name="event-agent",
        role="coder",
        backend=backend,
        model="mock-1",
    )

    events = await event_service.query(
        "test-scope",
        agent_id=record.id,
        type_filter="state_change",
    )
    assert len(events) >= 1
    assert events[0].type == "state_change"


@pytest.mark.asyncio
async def test_send_message_enqueues_inbox(
    agent_service, db: DatabaseManager
) -> None:
    """send_message() returns an inbox_id and the row is pending."""
    from sqlalchemy import text

    backend = MockBackend(transcript=[])
    record = await agent_service.create_agent(
        scope="test-scope",
        name="inbox-agent",
        role="coder",
        backend=backend,
        model="mock-1",
    )

    inbox_id = await agent_service.send_message(
        record.id, "orchestrator", "do something"
    )
    assert isinstance(inbox_id, int)
    assert inbox_id > 0

    # Verify the row is present and pending
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT status, to_agent_id, message FROM inbox WHERE id = :id"),
            {"id": inbox_id},
        ).fetchone()
    assert row is not None
    assert row.status == "pending"
    assert row.to_agent_id == record.id
    assert row.message == "do something"


@pytest.mark.asyncio
async def test_agent_processes_message_with_mock_backend(
    agent_service, event_service: EventService
) -> None:
    """Agent processes message end-to-end: idle after turn, agent_message emitted."""
    backend = MockBackend(
        transcript=[
            [
                TextChunk(text="done"),
                TurnEnd(
                    cost_usd=0.001,
                    input_tokens=50,
                    output_tokens=20,
                    context_pct=0.05,
                ),
            ]
        ]
    )
    record = await agent_service.create_agent(
        scope="test-scope",
        name="proc-agent",
        role="coder",
        backend=backend,
        model="mock-1",
    )

    await agent_service.send_message(record.id, "user", "please do something")

    # Wait for agent_message event to appear (turn completed)
    events = await wait_for_event(
        "test-scope", "agent_message", event_service, timeout=3.0
    )
    assert len(events) >= 1
    assert events[0].payload.get("text") == "done"

    # By the time agent_message is emitted the agent must be idle (or heading there)
    agent = await wait_for_status(record.id, "idle", agent_service, timeout=3.0)
    assert agent.status == "idle"


@pytest.mark.asyncio
async def test_interrupt_stops_turn(
    agent_service, event_service: EventService
) -> None:
    """interrupt_agent() stops an in-progress turn; agent returns to idle."""
    import asyncio

    # A multi-chunk transcript — we interrupt mid-turn
    # The mock backend respects interrupt() by stopping event iteration
    backend = MockBackend(
        transcript=[
            [
                TextChunk(text="chunk1"),
                TextChunk(text="chunk2"),
                TurnEnd(
                    cost_usd=0.001,
                    input_tokens=10,
                    output_tokens=5,
                    context_pct=0.01,
                ),
            ]
        ]
    )
    record = await agent_service.create_agent(
        scope="test-scope",
        name="interrupt-agent",
        role="coder",
        backend=backend,
        model="mock-1",
    )

    await agent_service.send_message(record.id, "user", "long task")

    # Let turn start — yield so session can pick up message and start running
    await asyncio.sleep(0.05)

    await agent_service.interrupt_agent(record.id)

    # After interrupt, agent should settle to idle (it may pass through "running")
    # We give it time to process and settle
    await asyncio.sleep(0.2)
    agent = await agent_service.get_agent(record.id)
    assert agent is not None
    assert agent.status in ("idle", "running")  # may still be winding down
    # Final check: must eventually be idle
    agent = await wait_for_status(record.id, "idle", agent_service, timeout=3.0)
    assert agent.status == "idle"


@pytest.mark.asyncio
async def test_archive_sets_status(
    agent_service, db: DatabaseManager
) -> None:
    """archive_agent() sets status=archived and stops the session."""
    from sqlalchemy import text

    backend = MockBackend(transcript=[])
    record = await agent_service.create_agent(
        scope="test-scope",
        name="archive-agent",
        role="coder",
        backend=backend,
        model="mock-1",
    )

    await agent_service.archive_agent(record.id)

    # Check from DB directly
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT status FROM agents WHERE id = :id"),
            {"id": record.id},
        ).fetchone()
    assert row is not None
    assert row.status == "archived"

    # Session task should be done
    session = agent_service._sessions.get(record.id)
    if session and session._task:
        assert session._task.done()


@pytest.mark.asyncio
async def test_restore_sessions_on_restart(
    db: DatabaseManager, event_service: EventService
) -> None:
    """restore_sessions() restarts sessions for all non-archived agents."""
    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService

    inbox = InboxService(db)

    # First "instance" — create an agent
    svc1 = AgentService(db, event_service, inbox)
    turn_end = TurnEnd(
        cost_usd=0.001, input_tokens=10, output_tokens=5, context_pct=0.01
    )
    backend1 = MockBackend(
        transcript=[[TextChunk(text="restored"), turn_end]]
    )
    record = await svc1.create_agent(
        scope="test-scope",
        name="restore-agent",
        role="coder",
        backend=backend1,
        model="mock-1",
    )
    # Stop old sessions (simulate process restart)
    old_session = svc1._sessions.get(record.id)
    if old_session and old_session._task and not old_session._task.done():
        old_session._task.cancel()
        try:
            await old_session._task
        except (asyncio.CancelledError, Exception):
            pass

    # Second "instance" — simulates a fresh process restart
    backend2 = MockBackend(
        transcript=[[TextChunk(text="restored"), turn_end]]
    )

    svc2 = AgentService(db, event_service, inbox)
    # Supply backends for restore (in real use, backends would be factory-created)
    await svc2.restore_sessions(backends={record.id: backend2})

    # Verify the session is live
    assert record.id in svc2._sessions

    # Send a message to confirm the session can receive messages
    await svc2.send_message(record.id, "orchestrator", "ping after restore")

    # Wait for agent_message event (confirms the turn ran)
    es_events = await wait_for_event(
        "test-scope", "agent_message", event_service, timeout=3.0
    )
    assert len(es_events) >= 1

    agent = await wait_for_status(record.id, "idle", svc2, timeout=3.0)
    assert agent.status == "idle"

    # Teardown svc2 sessions
    for session in svc2._sessions.values():
        task = session._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
