"""Tests for Task 5.3: Compaction + Memory (AC-007, AC-041–AC-044).

TDD: all tests are written FIRST — they should fail before any implementation.

Run: uv run pytest tests/test_compaction.py -q -m "not live and not slow"
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import Connection, text

from fleet.agents.backends.protocol import TextChunk, TurnEnd
from fleet.db import DatabaseManager, init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_compaction.db"))
    yield manager
    await manager.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_agent(agent_id: str, name: str, scope: str) -> Callable[[Connection], None]:
    """Return a callable that inserts a minimal agent row."""

    def _write(conn: Connection) -> None:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  created_at, updated_at)"
                " VALUES"
                " (:id, :name, :scope, :role, :backend, :model, 'idle',"
                "  :now, :now)"
            ),
            {
                "id": agent_id,
                "name": name,
                "scope": scope,
                "role": "worker",
                "backend": "mock",
                "model": "test",
                "now": now,
            },
        )
        conn.commit()

    return _write  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 1. test_memory_write_and_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_write_and_read(db: DatabaseManager) -> None:
    """write() inserts a row; read_recent() returns it with all fields correct."""
    from fleet.memory.service import MemoryService

    svc = MemoryService(db)
    memory_id = await svc.write(
        agent_id="agent-1",
        scope="scope-a",
        kind="compaction",
        content="This is a summary of the conversation.",
        metadata={"tokens_before": 90_000},
    )

    assert isinstance(memory_id, int)
    assert memory_id > 0

    records = await svc.read_recent("agent-1", "scope-a")
    assert len(records) == 1
    rec = records[0]
    assert rec.id == memory_id
    assert rec.agent_id == "agent-1"
    assert rec.scope == "scope-a"
    assert rec.kind == "compaction"
    assert rec.content == "This is a summary of the conversation."
    assert rec.metadata == {"tokens_before": 90_000}
    assert isinstance(rec.ts, str) and len(rec.ts) > 0


# ---------------------------------------------------------------------------
# 2. test_memory_read_filters_by_kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_read_filters_by_kind(db: DatabaseManager) -> None:
    """read_recent with kind filter returns only rows matching that kind."""
    from fleet.memory.service import MemoryService

    svc = MemoryService(db)
    await svc.write("agent-1", "scope-a", "compaction", "summary one")
    await svc.write("agent-1", "scope-a", "note", "a note")
    await svc.write("agent-1", "scope-a", "compaction", "summary two")

    compactions = await svc.read_recent("agent-1", "scope-a", kind="compaction")
    assert len(compactions) == 2
    assert all(r.kind == "compaction" for r in compactions)

    notes = await svc.read_recent("agent-1", "scope-a", kind="note")
    assert len(notes) == 1
    assert notes[0].kind == "note"

    all_records = await svc.read_recent("agent-1", "scope-a")
    assert len(all_records) == 3


# ---------------------------------------------------------------------------
# 3. test_compaction_triggers_at_threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_triggers_at_threshold(db: DatabaseManager) -> None:
    """AgentSession triggers compaction when cumulative tokens exceed threshold."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.budget import BudgetEnforcer
    from fleet.agents.inbox import InboxService
    from fleet.agents.session import AgentSession
    from fleet.events.service import EventService
    from fleet.events.sse import SSEHub
    from fleet.memory.service import MemoryService

    hub = SSEHub()
    event_svc = EventService(db, hub)
    inbox = InboxService(db)
    memory_svc = MemoryService(db)

    # Two turns each with 50 000 tokens → total 100 000 > threshold 80 000
    big_turn = [
        TextChunk(text="response"),
        TurnEnd(
            cost_usd=0.01,
            input_tokens=50_000,
            output_tokens=0,
            context_pct=0.25,
        ),
    ]
    backend = MockBackend(transcript=[big_turn, big_turn])

    agent_id = "agent-compact-1"
    scope = "scope-compact"
    await db.write(_insert_agent(agent_id, "compact-agent", scope))

    budget = BudgetEnforcer(
        agent_id=agent_id, scope=scope, db=db, event_service=event_svc
    )
    session = AgentSession(
        agent_id=agent_id,
        scope=scope,
        backend=backend,
        event_service=event_svc,
        inbox=inbox,
        db=db,
        budget=budget,
        memory_svc=memory_svc,
        compaction_threshold=80_000,
    )

    # Enqueue two messages
    await inbox.enqueue(agent_id, "user", "message one")
    await inbox.enqueue(agent_id, "user", "message two")

    # Run session until compaction fires or timeout
    task = asyncio.create_task(session.run())
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        events = await event_svc.query(
            scope, agent_id=agent_id, type_filter="state_change"
        )
        summaries = [e.summary for e in events]
        if "context_compacted" in summaries:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # Assert compaction event was emitted
    events = await event_svc.query(
        scope, agent_id=agent_id, type_filter="state_change"
    )
    compaction_events = [e for e in events if e.summary == "context_compacted"]
    assert len(compaction_events) >= 1, (
        "Expected a context_compacted state_change event"
    )

    payload = compaction_events[0].payload
    assert "tokens_before" in payload
    assert "memory_id" in payload

    # Assert memory row was written
    memories = await memory_svc.read_recent(agent_id, scope, kind="compaction")
    assert len(memories) >= 1

    # AC-007: summarize() must have received the actual conversation history,
    # not an empty list.
    assert backend.summarize_call_args is not None, (
        "Expected backend.summarize() to have been called"
    )
    assert len(backend.summarize_call_args) > 0, (
        "Expected backend.summarize() to receive non-empty conversation history"
    )


# ---------------------------------------------------------------------------
# 4. test_compaction_resets_token_counter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_resets_token_counter(db: DatabaseManager) -> None:
    """After compaction, token counter resets; second compaction at threshold again."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.budget import BudgetEnforcer
    from fleet.agents.inbox import InboxService
    from fleet.agents.session import AgentSession
    from fleet.events.service import EventService
    from fleet.events.sse import SSEHub
    from fleet.memory.service import MemoryService

    hub = SSEHub()
    event_svc = EventService(db, hub)
    inbox = InboxService(db)
    memory_svc = MemoryService(db)

    # Three turns: 50k, 50k (compaction at turn 2), 50k (reset → no 2nd)
    big_turn = [
        TextChunk(text="response"),
        TurnEnd(
            cost_usd=0.01,
            input_tokens=50_000,
            output_tokens=0,
            context_pct=0.25,
        ),
    ]
    backend = MockBackend(transcript=[big_turn, big_turn, big_turn])

    agent_id = "agent-compact-2"
    scope = "scope-compact-2"
    await db.write(_insert_agent(agent_id, "compact-agent-2", scope))

    budget = BudgetEnforcer(
        agent_id=agent_id, scope=scope, db=db, event_service=event_svc
    )
    session = AgentSession(
        agent_id=agent_id,
        scope=scope,
        backend=backend,
        event_service=event_svc,
        inbox=inbox,
        db=db,
        budget=budget,
        memory_svc=memory_svc,
        compaction_threshold=80_000,
    )

    # Enqueue 3 messages
    for i in range(3):
        await inbox.enqueue(agent_id, "user", f"message {i}")

    # Run all 3 turns
    task = asyncio.create_task(session.run())
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        msgs = await event_svc.query(
            scope, agent_id=agent_id, type_filter="agent_message"
        )
        if len(msgs) >= 3:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    # Only 1 compaction should have fired (after turn 2 at 100k tokens).
    # After reset, turn 3 adds 50k which is below the 80k threshold.
    memories = await memory_svc.read_recent(agent_id, scope, kind="compaction")
    assert len(memories) == 1, (
        f"Expected exactly 1 compaction, got {len(memories)}"
    )


# ---------------------------------------------------------------------------
# 5. test_restore_sessions_injects_prior_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_sessions_injects_prior_context(db: DatabaseManager) -> None:
    """restore_sessions loads prior compaction and passes it to the session."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService
    from fleet.events.service import EventService
    from fleet.events.sse import SSEHub
    from fleet.memory.service import MemoryService

    hub = SSEHub()
    event_svc = EventService(db, hub)
    inbox = InboxService(db)
    memory_svc = MemoryService(db)

    agent_id = "agent-restore-1"
    scope = "scope-restore"
    await db.write(_insert_agent(agent_id, "restore-agent", scope))

    # Write a compaction memory row for this agent
    prior_summary = "Prior context: agent worked on X and decided Y."
    await memory_svc.write(agent_id, scope, "compaction", prior_summary)

    # Create AgentService with memory_svc injected
    svc = AgentService(db, event_svc, inbox, memory_svc=memory_svc)

    # Provide a simple backend
    turn = [
        TextChunk(text="hi"),
        TurnEnd(cost_usd=0.0, input_tokens=10, output_tokens=5, context_pct=0.01),
    ]
    backend = MockBackend(transcript=[turn])

    await svc.restore_sessions(backends={agent_id: backend})

    # Give the session task a moment to start
    await asyncio.sleep(0.05)

    # The session should have been started; verify the prior context was loaded
    assert agent_id in svc._sessions
    session = svc._sessions[agent_id]
    assert session._prior_context == prior_summary

    # AC-043: prior_context must reach the assembled system prompt, not just be
    # stored as an attribute.  The session builds the prompt in __init__ so we
    # can inspect _system_prompt directly.
    assert prior_summary in session._system_prompt, (
        f"Expected prior_summary to appear in system prompt.\n"
        f"system_prompt={session._system_prompt!r}"
    )

    # Clean up
    await svc._stop_session(agent_id)


# ---------------------------------------------------------------------------
# 6. test_mock_backend_summarize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_backend_summarize() -> None:
    """MockBackend.summarize() returns the configured summary string."""
    from fleet.agents.backends.mock import MockBackend

    backend = MockBackend(transcript=[], mock_summary="[custom test summary]")
    messages: list[dict[str, object]] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    result = await backend.summarize(messages)
    assert result == "[custom test summary]"


@pytest.mark.asyncio
async def test_mock_backend_summarize_default() -> None:
    """MockBackend.summarize() returns '[mock summary]' when not configured."""
    from fleet.agents.backends.mock import MockBackend

    backend = MockBackend(transcript=[])
    result = await backend.summarize([])
    assert result == "[mock summary]"


# ---------------------------------------------------------------------------
# 7. test_memory_delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_delete(db: DatabaseManager) -> None:
    """delete() removes the row; subsequent read_recent returns empty list."""
    from fleet.memory.service import MemoryService

    svc = MemoryService(db)
    memory_id = await svc.write("agent-1", "scope-a", "compaction", "to be deleted")

    records_before = await svc.read_recent("agent-1", "scope-a")
    assert len(records_before) == 1

    await svc.delete(memory_id)

    records_after = await svc.read_recent("agent-1", "scope-a")
    assert len(records_after) == 0
