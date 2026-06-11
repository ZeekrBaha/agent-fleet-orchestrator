"""Backend protocol and event types for AgentBackend (Task 2.1, ADR-003).

Defines the abstract contract that all LLM-provider backends must satisfy.
MockBackend is the canonical test implementation; real backends (Anthropic,
OpenAI, etc.) follow the same interface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import (  # noqa: UP035 — Protocol/runtime_checkable used at runtime
    Protocol,
    runtime_checkable,
)

# ---------------------------------------------------------------------------
# Event dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TextChunk:
    """A streamed text fragment from the model."""

    text: str


@dataclass
class ToolUseEvent:
    """The model is invoking a tool."""

    tool_id: str        # unique id for this call
    tool_name: str
    input: dict[str, object]


@dataclass
class ToolResultEvent:
    """Result of a tool call (already recorded in transcript for MockBackend)."""

    tool_id: str
    output: str
    is_error: bool


@dataclass
class TurnEnd:
    """Signals that the model's current turn is complete."""

    cost_usd: float
    input_tokens: int
    output_tokens: int
    context_pct: float  # 0.0–1.0; ratio of context window used


@dataclass
class BackendError:
    """A recoverable or non-recoverable backend error."""

    message: str
    retryable: bool


# Union type for events emitted during a backend turn stream
BackendEvent = TextChunk | ToolUseEvent | ToolResultEvent | TurnEnd | BackendError


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentBackend(Protocol):
    """Minimal contract for a streaming LLM agent backend.

    Implementations are async; *events()* is an async generator.
    All session state is keyed by an opaque *session_ref* string.
    """

    async def start(self, session_ref: str | None = None) -> str:
        """Start or resume a session.

        Args:
            session_ref: If provided, resume an existing session; otherwise
                create a new one.

        Returns:
            The session_ref (opaque string) to pass to subsequent calls.
        """
        ...

    async def send(self, session_ref: str, message: str) -> None:
        """Inject a user message into the session.

        Advances the transcript to the next turn so that the following
        *events()* call replays the corresponding events.
        """
        ...

    def events(self, session_ref: str) -> AsyncIterator[BackendEvent]:
        """Stream events from the current turn.

        Yields events in order until *TurnEnd* or *BackendError*.
        Implementations use ``async def events(...) -> AsyncIterator[...]``
        with ``yield`` (async generator method).
        """
        ...

    async def interrupt(self, session_ref: str) -> None:
        """Interrupt the current turn. Safe to call when idle."""
        ...

    async def stop(self, session_ref: str) -> None:
        """Permanently stop and clean up the session."""
        ...

    async def inject_tool_result(
        self,
        session_ref: str,
        tool_id: str,
        output: str,
        is_error: bool = False,
    ) -> None:
        """Send a tool result back to the agent after a *ToolUseEvent*.

        MockBackend treats this as a no-op because results are already
        embedded in the transcript fixture.
        """
        ...

    async def summarize(self, messages: list[dict[str, object]]) -> str:
        """Request a condensed summary of *messages* from the backend.

        Args:
            messages: The conversation history to summarise (list of
                      {"role": ..., "content": ...} dicts).

        Returns:
            A plain-text summary string.
        """
        ...

    async def reset_history(self, session_ref: str, summary: str) -> None:
        """Reset the in-memory message history after a compaction cycle.

        Clears the backend's per-session message list and optionally re-seeds
        it with a compact context derived from *summary*.

        Args:
            session_ref: The session whose history to reset.
            summary:     Compacted summary text.  Empty string → just clear.
        """
        ...
