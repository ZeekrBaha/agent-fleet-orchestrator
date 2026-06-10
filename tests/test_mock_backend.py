"""Tests for AgentBackend protocol + MockBackend (Task 2.1).

TDD: tests written FIRST; implementation follows.

Run:  uv run pytest tests/test_mock_backend.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Fixtures dir so tests can resolve transcript paths without hard-coding CWD
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "transcripts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect_events(backend, session_ref: str) -> list:
    """Drain all events from one turn."""
    events = []
    async for event in backend.events(session_ref):
        events.append(event)
    return events


def _noop_turn_end():
    from fleet.agents.backends.protocol import TurnEnd

    return TurnEnd(
        cost_usd=0.0, input_tokens=0, output_tokens=0, context_pct=0.0
    )


# ---------------------------------------------------------------------------
# 1. start() — session lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_returns_session_ref() -> None:
    """start() with no args returns a non-empty string session ref."""
    from fleet.agents.backends.mock import MockBackend

    backend = MockBackend(transcript=[[_noop_turn_end()]])
    ref = await backend.start()
    assert isinstance(ref, str)
    assert len(ref) > 0


@pytest.mark.asyncio
async def test_start_returns_mock_prefixed_ref() -> None:
    """start() with no args returns a ref prefixed with 'mock-'."""
    from fleet.agents.backends.mock import MockBackend

    backend = MockBackend(transcript=[[_noop_turn_end()]])
    ref = await backend.start()
    assert ref.startswith("mock-")


@pytest.mark.asyncio
async def test_resume_returns_same_ref() -> None:
    """start(session_ref='existing') returns 'existing' unchanged."""
    from fleet.agents.backends.mock import MockBackend

    backend = MockBackend(transcript=[[_noop_turn_end()]])
    ref = await backend.start(session_ref="existing")
    assert ref == "existing"


# ---------------------------------------------------------------------------
# 2. events() — replay correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_yields_all_turn_events() -> None:
    """Replay simple_task.jsonl: assert event types match expected sequence."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.backends.protocol import (
        TextChunk,
        ToolResultEvent,
        ToolUseEvent,
        TurnEnd,
    )

    backend = MockBackend(transcript_path=FIXTURES_DIR / "simple_task.jsonl")
    ref = await backend.start()
    await backend.send(ref, "do the task")
    events = await _collect_events(backend, ref)

    assert len(events) == 5
    assert isinstance(events[0], TextChunk)
    assert isinstance(events[1], ToolUseEvent)
    assert isinstance(events[2], ToolResultEvent)
    assert isinstance(events[3], TextChunk)
    assert isinstance(events[4], TurnEnd)


@pytest.mark.asyncio
async def test_transcript_replay_deterministic() -> None:
    """Replay simple_task.jsonl twice; both runs yield identical events."""
    from fleet.agents.backends.mock import MockBackend

    def _make_backend():
        return MockBackend(transcript_path=FIXTURES_DIR / "simple_task.jsonl")

    async def _run():
        b = _make_backend()
        ref = await b.start()
        await b.send(ref, "go")
        return await _collect_events(b, ref)

    run1 = await _run()
    run2 = await _run()

    assert len(run1) == len(run2)
    for e1, e2 in zip(run1, run2, strict=True):
        assert isinstance(e1, type(e2))
        assert vars(e1) == vars(e2)


@pytest.mark.asyncio
async def test_multi_turn_replay() -> None:
    """Replay multi_turn.jsonl: turn 1 has 2 events, turn 2 has 4 events."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.backends.protocol import (
        TextChunk,
        ToolResultEvent,
        ToolUseEvent,
        TurnEnd,
    )

    backend = MockBackend(transcript_path=FIXTURES_DIR / "multi_turn.jsonl")
    ref = await backend.start()

    # --- Turn 1 ---
    await backend.send(ref, "start")
    turn1 = await _collect_events(backend, ref)
    assert len(turn1) == 2
    assert isinstance(turn1[0], TextChunk)
    assert isinstance(turn1[1], TurnEnd)

    # --- Turn 2 ---
    await backend.send(ref, "continue")
    turn2 = await _collect_events(backend, ref)
    assert len(turn2) == 4
    assert isinstance(turn2[0], TextChunk)
    assert isinstance(turn2[1], ToolUseEvent)
    assert isinstance(turn2[2], ToolResultEvent)
    assert isinstance(turn2[3], TurnEnd)


# ---------------------------------------------------------------------------
# 3. Inline transcript (list[list[BackendEvent]])
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_transcript_replay() -> None:
    """MockBackend accepts pre-built list of turns directly."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.backends.protocol import TextChunk, TurnEnd

    turn = [
        TextChunk(text="hello"),
        TurnEnd(cost_usd=0.001, input_tokens=10, output_tokens=5, context_pct=0.1),
    ]
    backend = MockBackend(transcript=[turn])
    ref = await backend.start()
    await backend.send(ref, "hi")
    events = await _collect_events(backend, ref)

    assert len(events) == 2
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "hello"
    assert isinstance(events[1], TurnEnd)
    assert events[1].cost_usd == 0.001


# ---------------------------------------------------------------------------
# 4. interrupt()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_stops_event_stream() -> None:
    """After interrupt(), events() yields nothing for that session."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.backends.protocol import TextChunk, TurnEnd

    turns = [
        [
            TextChunk(text="a"),
            TextChunk(text="b"),
            TurnEnd(
                cost_usd=0.0, input_tokens=0, output_tokens=0, context_pct=0.0
            ),
        ],
    ]
    backend = MockBackend(transcript=turns)
    ref = await backend.start()
    await backend.send(ref, "go")

    # Interrupt before draining
    await backend.interrupt(ref)

    events = await _collect_events(backend, ref)
    assert len(events) == 0


# ---------------------------------------------------------------------------
# 5. stop()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cleans_up_session() -> None:
    """After stop(), the session ref is no longer tracked."""
    from fleet.agents.backends.mock import MockBackend

    backend = MockBackend(transcript=[[_noop_turn_end()]])
    ref = await backend.start()
    await backend.stop(ref)

    # After stop, internal state should not contain this session
    assert ref not in backend._sessions  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 6. inject_tool_result() — no-op in mock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_tool_result_is_noop() -> None:
    """inject_tool_result() does not raise and does not affect transcript replay."""
    from fleet.agents.backends.mock import MockBackend

    backend = MockBackend(transcript=[[_noop_turn_end()]])
    ref = await backend.start()
    # Should not raise
    await backend.inject_tool_result(ref, tool_id="t1", output="ok", is_error=False)


# ---------------------------------------------------------------------------
# 7. JSONL file loading — cost_usd value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_from_file_turn_end_values() -> None:
    """simple_task.jsonl TurnEnd: cost_usd=0.002, input_tokens=150, output_tokens=80."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.backends.protocol import TurnEnd

    backend = MockBackend(transcript_path=FIXTURES_DIR / "simple_task.jsonl")
    ref = await backend.start()
    await backend.send(ref, "run")
    events = await _collect_events(backend, ref)

    turn_end = events[-1]
    assert isinstance(turn_end, TurnEnd)
    assert turn_end.cost_usd == 0.002
    assert turn_end.input_tokens == 150
    assert turn_end.output_tokens == 80
    assert turn_end.context_pct == pytest.approx(0.12)


# ---------------------------------------------------------------------------
# 8. Protocol conformance
# ---------------------------------------------------------------------------


def test_mock_backend_implements_protocol() -> None:
    """MockBackend is recognised as an AgentBackend via runtime_checkable Protocol."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.backends.protocol import AgentBackend

    backend = MockBackend(transcript=[[_noop_turn_end()]])
    assert isinstance(backend, AgentBackend)


# ---------------------------------------------------------------------------
# 9. No more turns — extra send() is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extra_send_after_transcript_exhausted_is_noop() -> None:
    """Calling send() after all turns are consumed yields empty events()."""
    from fleet.agents.backends.mock import MockBackend

    backend = MockBackend(transcript=[[_noop_turn_end()]])
    ref = await backend.start()

    # Consume the single turn
    await backend.send(ref, "first")
    await _collect_events(backend, ref)

    # Now call send again — no more turns
    await backend.send(ref, "second")
    events = await _collect_events(backend, ref)
    assert events == []
