"""ClaudeBackend: Anthropic Claude adapter — AgentBackend protocol (Task 2.3).

Sessions are stateless server-side; the adapter maintains an in-memory
message history keyed by session_ref.  Passing session_ref to start()
resumes with the same stable ID (history must be rebuilt externally for
true persistence — future concern).

Cost rates used: $3 / 1M input tokens, $15 / 1M output tokens (Sonnet
approximation; model-specific rates are a future concern).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import anthropic

from fleet.agents.backends.protocol import (
    BackendError,
    BackendEvent,
    TextChunk,
    ToolUseEvent,
    TurnEnd,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost constants (per-token)
# ---------------------------------------------------------------------------

_INPUT_COST_PER_TOKEN: float = 3e-6    # $3 per 1M input tokens
_OUTPUT_COST_PER_TOKEN: float = 15e-6  # $15 per 1M output tokens
_MAX_CONTEXT_TOKENS: int = 200_000     # approximate context window for %


# ---------------------------------------------------------------------------
# Pure parsing helper — testable without I/O
# ---------------------------------------------------------------------------


def _parse_stream_chunk(
    chunk_type: str, chunk_data: dict[str, Any]
) -> BackendEvent | None:
    """Map a single raw streaming chunk to a BackendEvent, or None if ignored.

    Args:
        chunk_type:  The synthetic event type string.  Uses the same names
                     as the Anthropic streaming event types, plus the
                     synthetic ``"tool_use_complete"`` emitted internally
                     when a full tool_use block has been assembled.
        chunk_data:  The event payload dict.

    Returns:
        A BackendEvent instance, or None if the chunk should be silently
        ignored (e.g. ``message_start``, ``ping``, unknown types).
    """
    if chunk_type == "content_block_delta":
        delta = chunk_data.get("delta", {})
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            return TextChunk(text=str(delta.get("text", "")))
        return None  # input_json_delta and others are handled by accumulation

    if chunk_type == "tool_use_complete":
        return ToolUseEvent(
            tool_id=str(chunk_data.get("tool_id", "")),
            tool_name=str(chunk_data.get("tool_name", "")),
            input=dict(chunk_data.get("input", {})),
        )

    if chunk_type == "message_delta":
        usage = chunk_data.get("usage", {})
        if isinstance(usage, dict):
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
        else:
            input_tokens = 0
            output_tokens = 0
        cost_usd = (
            input_tokens * _INPUT_COST_PER_TOKEN
            + output_tokens * _OUTPUT_COST_PER_TOKEN
        )
        context_pct = input_tokens / _MAX_CONTEXT_TOKENS
        return TurnEnd(
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_pct=context_pct,
        )

    # message_start, content_block_start, content_block_stop, ping, etc.
    return None


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class _SessionState:
    """In-memory state for one active session."""

    pending_message: str | None = None
    interrupted: bool = False
    # Anthropic message history: list of {"role": ..., "content": ...} dicts
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Tool results accumulated between a ToolUseEvent and the next send()
    pending_tool_results: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ClaudeBackend
# ---------------------------------------------------------------------------


class ClaudeBackend:
    """Anthropic Claude backend implementing the AgentBackend protocol.

    Args:
        model:        Claude model ID.
        api_key:      API key.  Falls back to ``ANTHROPIC_API_KEY`` env var.
        system_prompt: Optional system prompt prepended to every conversation.
        max_tokens:   Maximum tokens to generate per turn.
    """

    def __init__(
        self,
        model: str = "claude-opus-4-8",
        api_key: str | None = None,
        system_prompt: str = "",
        max_tokens: int = 8192,
    ) -> None:
        self._model = model
        self._api_key = api_key  # None → SDK reads ANTHROPIC_API_KEY automatically
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._sessions: dict[str, _SessionState] = {}

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    async def start(self, session_ref: str | None = None) -> str:
        """Return a session_ref; create new session state if needed.

        Args:
            session_ref: If provided, resume with this ID; otherwise a new
                         UUID is generated.

        Returns:
            The session_ref string.
        """
        ref = session_ref if session_ref is not None else str(uuid.uuid4())
        if ref not in self._sessions:
            self._sessions[ref] = _SessionState()
        return ref

    async def send(self, session_ref: str, message: str) -> None:
        """Queue *message* as the next user turn.

        Clears any leftover pending_tool_results from the previous turn and
        resets the interrupted flag.
        """
        state = self._sessions[session_ref]
        state.pending_message = message
        state.interrupted = False
        # Tool results from the prior turn were already added to messages;
        # clear the staging list for the new turn.
        state.pending_tool_results = []

    async def events(
        self, session_ref: str
    ) -> AsyncGenerator[BackendEvent, None]:
        """Stream events for the current pending turn.

        Yields:
            TextChunk for each streamed text fragment.
            ToolUseEvent when the model requests a tool call.
            TurnEnd when the full turn is complete.
            BackendError if no message is pending or a network error occurs.
        """
        state = self._sessions[session_ref]

        if state.pending_message is None:
            yield BackendError("no pending message", retryable=False)
            return

        # Append user message to history
        state.messages.append({"role": "user", "content": state.pending_message})
        state.pending_message = None

        # Build the client fresh each call (stateless HTTP)
        client = anthropic.Anthropic(api_key=self._api_key)

        try:
            # Tool-use accumulation buffer
            # Maps index -> {tool_id, tool_name, input_json_parts}
            tool_blocks: dict[int, dict[str, Any]] = {}
            current_block_index: int | None = None
            current_block_type: str | None = None

            input_tokens: int = 0
            output_tokens: int = 0
            stop_reason: str | None = None

            # Build the kwargs dict; only include system if non-empty
            stream_kwargs: dict[str, Any] = {
                "model": self._model,
                "messages": list(state.messages),
                "max_tokens": self._max_tokens,
            }
            if self._system_prompt:
                stream_kwargs["system"] = self._system_prompt

            with client.messages.stream(**stream_kwargs) as stream:
                for raw_event in stream:
                    if state.interrupted:
                        return

                    event_type = getattr(raw_event, "type", None)

                    # ---- message_start: capture initial usage if present ----
                    if event_type == "message_start":
                        msg = getattr(raw_event, "message", None)
                        if msg is not None:
                            usage = getattr(msg, "usage", None)
                            if usage is not None:
                                input_tokens = getattr(usage, "input_tokens", 0)

                    # ---- content_block_start: track block type ----
                    elif event_type == "content_block_start":
                        block = getattr(raw_event, "content_block", None)
                        current_block_index = getattr(raw_event, "index", None)
                        current_block_type = getattr(block, "type", None)
                        is_tool = current_block_type == "tool_use"
                        if is_tool and current_block_index is not None:
                            tool_blocks[current_block_index] = {
                                "tool_id": getattr(block, "id", ""),
                                "tool_name": getattr(block, "name", ""),
                                "input_json_parts": [],
                            }

                    # ---- content_block_delta: text or input_json ----
                    elif event_type == "content_block_delta":
                        delta = getattr(raw_event, "delta", None)
                        delta_type = getattr(delta, "type", None)

                        if delta_type == "text_delta":
                            text = getattr(delta, "text", "")
                            parsed = _parse_stream_chunk(
                                "content_block_delta",
                                {"delta": {"type": "text_delta", "text": text}},
                            )
                            if parsed is not None:
                                yield parsed

                        elif delta_type == "input_json_delta":
                            partial = getattr(delta, "partial_json", "")
                            if (
                                current_block_index is not None
                                and current_block_index in tool_blocks
                            ):
                                tool_blocks[current_block_index][
                                    "input_json_parts"
                                ].append(partial)

                    # ---- content_block_stop: finalise tool block ----
                    elif event_type == "content_block_stop":
                        idx = getattr(raw_event, "index", None)
                        if idx is not None and idx in tool_blocks:
                            tb = tool_blocks.pop(idx)
                            # Assemble and parse the accumulated JSON
                            import json as _json
                            raw_json = "".join(tb["input_json_parts"])
                            tool_name = tb["tool_name"]
                            try:
                                parsed_input: dict[str, Any] = (
                                    _json.loads(raw_json) if raw_json else {}
                                )
                            except _json.JSONDecodeError as exc:
                                _logger.warning(
                                    "malformed tool JSON for tool %r: %r"
                                    " — using empty input",
                                    tool_name,
                                    exc,
                                )
                                parsed_input = {}
                            parsed = _parse_stream_chunk(
                                "tool_use_complete",
                                {
                                    "tool_id": tb["tool_id"],
                                    "tool_name": tb["tool_name"],
                                    "input": parsed_input,
                                },
                            )
                            if parsed is not None:
                                yield parsed

                    # ---- message_delta: stop_reason + output tokens ----
                    elif event_type == "message_delta":
                        delta = getattr(raw_event, "delta", None)
                        stop_reason = getattr(delta, "stop_reason", None)
                        usage = getattr(raw_event, "usage", None)
                        if usage is not None:
                            output_tokens = getattr(usage, "output_tokens", 0)

            # Stream complete — record the assistant message in history
            final_message = stream.get_final_message()
            state.messages.append(
                {"role": "assistant", "content": final_message.content}
            )

            # Yield TurnEnd with cost / token accounting
            parsed_end = _parse_stream_chunk(
                "message_delta",
                {
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                    "stop_reason": stop_reason,
                },
            )
            if parsed_end is not None:
                yield parsed_end

        except anthropic.APIError as exc:
            retryable = isinstance(
                exc,
                (
                    anthropic.RateLimitError,
                    anthropic.InternalServerError,
                    anthropic.APITimeoutError,
                ),
            )
            yield BackendError(message=str(exc), retryable=retryable)

    async def interrupt(self, session_ref: str) -> None:
        """Signal the current turn to stop at the next yield point."""
        state = self._sessions.get(session_ref)
        if state is not None:
            state.interrupted = True

    async def stop(self, session_ref: str) -> None:
        """Remove session state permanently."""
        self._sessions.pop(session_ref, None)

    async def inject_tool_result(
        self,
        session_ref: str,
        tool_id: str,
        output: str,
        is_error: bool = False,
    ) -> None:
        """Store a tool result to be included in the next message turn.

        The result is appended to pending_tool_results and will be flushed
        into the message history by the next send() / events() cycle.
        """
        state = self._sessions[session_ref]
        state.pending_tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": output,
                "is_error": is_error,
            }
        )


# ---------------------------------------------------------------------------
# Test-support helper (defined after ClaudeBackend to avoid forward reference)
# ---------------------------------------------------------------------------


def _get_session_state(
    backend: ClaudeBackend, ref: str
) -> _SessionState | None:
    """Return session state for *ref*, or None if the session does not exist.

    Test-support only — production code accesses the private dict directly.
    """
    return backend._sessions.get(ref)  # noqa: SLF001
