"""Tests for ClaudeBackend adapter (Task 2.3).

TDD: offline unit tests are written first. Live smoke test requires
ANTHROPIC_API_KEY and is skipped automatically when it is absent.

Run (offline):   uv run pytest tests/test_claude_adapter.py -q -m "not live"
Run (live):      ANTHROPIC_API_KEY=sk-... pytest tests/test_claude_adapter.py -m live

"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect_events(backend, session_ref: str) -> list:
    """Drain all events from one turn into a list."""
    events = []
    async for event in backend.events(session_ref):
        events.append(event)
    return events


# ---------------------------------------------------------------------------
# 1. _parse_stream_chunk — pure event normalisation (no API calls)
# ---------------------------------------------------------------------------


def test_text_delta_maps_to_text_chunk() -> None:
    """`content_block_delta` with a text_delta maps to TextChunk."""
    from fleet.agents.backends.claude import _parse_stream_chunk
    from fleet.agents.backends.protocol import TextChunk

    result = _parse_stream_chunk(
        "content_block_delta",
        {"delta": {"type": "text_delta", "text": "hello"}},
    )
    assert result == TextChunk(text="hello")


def test_tool_use_block_maps_to_tool_use_event() -> None:
    """Completed tool_use block data maps to ToolUseEvent."""
    from fleet.agents.backends.claude import _parse_stream_chunk
    from fleet.agents.backends.protocol import ToolUseEvent

    result = _parse_stream_chunk(
        "tool_use_complete",
        {
            "tool_id": "toolu_01",
            "tool_name": "read_file",
            "input": {"path": "/tmp/foo"},
        },
    )
    assert result == ToolUseEvent(
        tool_id="toolu_01",
        tool_name="read_file",
        input={"path": "/tmp/foo"},
    )


def test_message_delta_end_maps_to_turn_end() -> None:
    """`message_delta` with usage data produces TurnEnd with correct cost."""
    from fleet.agents.backends.claude import _parse_stream_chunk
    from fleet.agents.backends.protocol import TurnEnd

    result = _parse_stream_chunk(
        "message_delta",
        {
            "usage": {"input_tokens": 1000, "output_tokens": 200},
            "stop_reason": "end_turn",
        },
    )
    assert isinstance(result, TurnEnd)
    assert result.input_tokens == 1000
    assert result.output_tokens == 200
    # cost = 1000 * 0.000003 + 200 * 0.000015 = 0.003 + 0.003 = 0.006
    assert abs(result.cost_usd - 0.006) < 1e-9
    # context_pct = 1000 / 200_000 = 0.005
    assert abs(result.context_pct - 0.005) < 1e-9


def test_parse_stream_chunk_unknown_returns_none() -> None:
    """Unknown chunk types return None (ignored by the stream loop)."""
    from fleet.agents.backends.claude import _parse_stream_chunk

    result = _parse_stream_chunk("message_start", {"model": "claude-opus-4-8"})
    assert result is None


def test_parse_stream_chunk_non_text_delta_returns_none() -> None:
    """content_block_delta with non-text_delta type returns None."""
    from fleet.agents.backends.claude import _parse_stream_chunk

    result = _parse_stream_chunk(
        "content_block_delta",
        {"delta": {"type": "input_json_delta", "partial_json": '{"k'}},
    )
    assert result is None


# ---------------------------------------------------------------------------
# 2. start() / resume semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_returns_uuid_session_ref() -> None:
    """start() with no args returns a non-empty string."""
    from fleet.agents.backends.claude import ClaudeBackend

    backend = ClaudeBackend()
    ref = await backend.start()
    assert isinstance(ref, str)
    assert len(ref) > 0


@pytest.mark.asyncio
async def test_resume_returns_same_ref() -> None:
    """start(existing_ref) returns the same ref unchanged."""
    from fleet.agents.backends.claude import ClaudeBackend

    backend = ClaudeBackend()
    ref = await backend.start("my-existing-session-id")
    assert ref == "my-existing-session-id"


# ---------------------------------------------------------------------------
# 3. send() / session state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_stores_pending_message() -> None:
    """After send(), the session's pending_message is set."""
    from fleet.agents.backends.claude import ClaudeBackend, _get_session_state

    backend = ClaudeBackend()
    ref = await backend.start()
    await backend.send(ref, "hello world")
    state = _get_session_state(backend, ref)
    assert state.pending_message == "hello world"


@pytest.mark.asyncio
async def test_events_yields_backend_error_when_no_pending_message() -> None:
    """events() without a prior send() yields BackendError(retryable=False)."""
    from fleet.agents.backends.claude import ClaudeBackend
    from fleet.agents.backends.protocol import BackendError

    backend = ClaudeBackend()
    ref = await backend.start()
    events = await _collect_events(backend, ref)
    assert len(events) == 1
    assert isinstance(events[0], BackendError)
    assert events[0].retryable is False


@pytest.mark.asyncio
async def test_inject_tool_result_stores_result() -> None:
    """inject_tool_result() appends to pending_tool_results."""
    from fleet.agents.backends.claude import ClaudeBackend, _get_session_state

    backend = ClaudeBackend()
    ref = await backend.start()
    await backend.inject_tool_result(ref, "toolu_01", "file contents", is_error=False)
    state = _get_session_state(backend, ref)
    assert len(state.pending_tool_results) == 1
    result = state.pending_tool_results[0]
    assert result["tool_use_id"] == "toolu_01"
    assert result["content"] == "file contents"
    assert result["type"] == "tool_result"


@pytest.mark.asyncio
async def test_stop_removes_session() -> None:
    """stop() removes session state so it no longer exists."""
    from fleet.agents.backends.claude import ClaudeBackend, _get_session_state

    backend = ClaudeBackend()
    ref = await backend.start()
    await backend.stop(ref)
    state = _get_session_state(backend, ref)
    assert state is None


# ---------------------------------------------------------------------------
# 4. Live smoke test (requires ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_smoke() -> None:
    """Send a real message and verify at least one TextChunk + one TurnEnd."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set — skipping live test")

    from fleet.agents.backends.claude import ClaudeBackend
    from fleet.agents.backends.protocol import TextChunk, TurnEnd

    backend = ClaudeBackend(model="claude-sonnet-4-5")
    ref = await backend.start()
    await backend.send(ref, "Say exactly: OK")
    events = await _collect_events(backend, ref)

    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    turn_ends = [e for e in events if isinstance(e, TurnEnd)]

    assert len(text_chunks) >= 1, "expected at least one TextChunk"
    assert len(turn_ends) == 1, "expected exactly one TurnEnd"
    assert turn_ends[0].cost_usd > 0, "expected positive cost"
    await backend.stop(ref)
