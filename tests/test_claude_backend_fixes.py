"""Tests for ClaudeBackend bug fixes: P1-15 (async client), P1-16 (tool results),
P1-17 (reset_history / compaction).

Written test-first per the project TDD mandate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import anthropic

from fleet.agents.backends.claude import ClaudeBackend, _get_session_state

# ---------------------------------------------------------------------------
# P1-15: ClaudeBackend must use AsyncAnthropic, not sync Anthropic
# ---------------------------------------------------------------------------


def test_claude_backend_creates_async_client() -> None:
    """P1-15: __init__ must store an AsyncAnthropic client on self._client."""
    backend = ClaudeBackend(api_key="test-key")
    assert hasattr(backend, "_client"), "ClaudeBackend must have a _client attribute"
    assert isinstance(  # noqa: SLF001
        backend._client, anthropic.AsyncAnthropic
    ), f"Expected AsyncAnthropic, got {type(backend._client)}"


def test_claude_backend_not_sync_client() -> None:
    """P1-15: _client must NOT be the blocking sync Anthropic class."""
    backend = ClaudeBackend(api_key="test-key")
    assert not isinstance(  # noqa: SLF001
        backend._client, anthropic.Anthropic
    ), "_client must not be the sync Anthropic class (event-loop-blocking)"


# ---------------------------------------------------------------------------
# P1-16: Tool results must not be dropped on send()
# ---------------------------------------------------------------------------


async def test_tool_results_flushed_into_messages() -> None:
    """P1-16: inject_tool_result → send() → events() must include tool_result block."""
    backend = ClaudeBackend(api_key="test-key")
    ref = await backend.start()

    # Simulate: model asked for a tool in the previous turn; result injected.
    await backend.inject_tool_result(ref, tool_id="tid-1", output="result-value")

    # Send next user message — this must NOT drop the tool result.
    await backend.send(ref, "continue with the tool result")

    # Capture the messages list that events() would pass to the API.
    captured: list[list[dict]] = []  # list of messages lists from each stream call

    fake_final = MagicMock()
    fake_final.content = []

    class _FakeStream:
        async def __aenter__(self) -> _FakeStream:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def __aiter__(self) -> _FakeStream:
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

        async def get_final_message(self) -> MagicMock:
            return fake_final

    def _fake_stream(**kwargs: object) -> _FakeStream:  # type: ignore[return]
        captured.append(list(kwargs.get("messages", [])))  # type: ignore[arg-type]
        return _FakeStream()  # type: ignore[return-value]

    backend._client.messages.stream = _fake_stream  # type: ignore[assignment]  # noqa: SLF001

    async for _ in backend.events(ref):
        pass

    assert captured, "stream() was never called — events() did not reach the API"

    messages_sent = captured[0]
    # There must be a user turn containing tool_result content blocks.
    tool_result_turns = [
        m
        for m in messages_sent
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in m["content"]
        )
    ]
    assert tool_result_turns, (
        "No user turn with tool_result blocks found.\n"
        f"Messages sent to API: {messages_sent}"
    )
    block = tool_result_turns[0]["content"][0]
    assert block["tool_use_id"] == "tid-1"
    assert block["content"] == "result-value"


async def test_send_does_not_clear_pending_tool_results() -> None:
    """P1-16: send() must not wipe pending_tool_results before events() flushes them."""
    backend = ClaudeBackend(api_key="test-key")
    ref = await backend.start()

    await backend.inject_tool_result(ref, tool_id="t2", output="out")
    state = _get_session_state(backend, ref)
    assert state is not None

    # send() must leave pending_tool_results intact for events() to flush.
    await backend.send(ref, "next message")
    assert len(state.pending_tool_results) == 1, (
        "send() must not clear pending_tool_results; "
        "events() needs them to build the tool-result user turn"
    )


# ---------------------------------------------------------------------------
# P1-17: reset_history clears and re-seeds with summary context
# ---------------------------------------------------------------------------


async def test_reset_history_clears_and_reseeds() -> None:
    """P1-17: reset_history(ref, summary) replaces messages with seed context pair."""
    backend = ClaudeBackend(api_key="test-key")
    ref = await backend.start()

    state = _get_session_state(backend, ref)
    assert state is not None

    # Populate some history to simulate a long conversation.
    state.messages.extend(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "done"},
        ]
    )
    assert len(state.messages) == 4

    await backend.reset_history(ref, "Summary: agent greeted and completed a task.")

    # Must be exactly two entries: the compacted-context seed pair.
    assert len(state.messages) == 2, (
        f"Expected 2 seed messages, got {len(state.messages)}: {state.messages}"
    )
    assert state.messages[0] == {"role": "user", "content": "[Context compacted]"}
    assert state.messages[1] == {
        "role": "assistant",
        "content": "Summary: agent greeted and completed a task.",
    }


async def test_reset_history_empty_summary_clears_messages() -> None:
    """P1-17: reset_history with empty summary clears messages without seeding."""
    backend = ClaudeBackend(api_key="test-key")
    ref = await backend.start()

    state = _get_session_state(backend, ref)
    assert state is not None
    state.messages.append({"role": "user", "content": "anything"})

    await backend.reset_history(ref, "")

    assert state.messages == [], f"Expected empty messages, got: {state.messages}"


async def test_reset_history_unknown_ref_is_noop() -> None:
    """P1-17: reset_history on an unknown session_ref must not raise."""
    backend = ClaudeBackend(api_key="test-key")
    # No session created — must not raise.
    await backend.reset_history("no-such-ref", "anything")


# ---------------------------------------------------------------------------
# P1-17: AgentBackend protocol and MockBackend must expose reset_history
# ---------------------------------------------------------------------------


def test_protocol_has_reset_history() -> None:
    """P1-17: AgentBackend protocol must declare reset_history."""
    from fleet.agents.backends.protocol import AgentBackend

    assert hasattr(AgentBackend, "reset_history"), (
        "AgentBackend protocol must declare reset_history"
    )


async def test_mock_backend_reset_history_noop() -> None:
    """P1-17: MockBackend.reset_history must be a no-op (mock has no real history)."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.backends.protocol import TurnEnd

    turn = TurnEnd(cost_usd=0.0, input_tokens=0, output_tokens=0, context_pct=0.0)
    mock = MockBackend(transcript=[[turn]])
    ref = await mock.start()
    # Must not raise; return value is None.
    result = await mock.reset_history(ref, "some summary")
    assert result is None
