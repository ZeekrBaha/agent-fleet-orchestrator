"""MockBackend: deterministic JSONL transcript replay for agent tests (Task 2.1).

Replays pre-recorded BackendEvent sequences without requiring real API keys.
Accepts either an inline list-of-turns or a JSONL file path.

JSONL line format — each line is a JSON object with a ``type`` field:
    {"type": "text_chunk", "text": "..."}
    {"type": "tool_use", "tool_id": "t1", "tool_name": "...", "input": {...}}
    {"type": "tool_result", "tool_id": "t1", "output": "...", "is_error": false}
    {"type": "turn_end", "cost_usd": 0.001, "input_tokens": 100,
                         "output_tokens": 50, "context_pct": 0.15}
    {"type": "error", "message": "...", "retryable": true}

Turns are delimited by ``turn_end`` (or ``error``) lines.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path

from fleet.agents.backends.protocol import (
    BackendError,
    BackendEvent,
    TextChunk,
    ToolResultEvent,
    ToolUseEvent,
    TurnEnd,
)

# ---------------------------------------------------------------------------
# JSONL parsing
# ---------------------------------------------------------------------------


def _require(obj: dict[str, object], key: str) -> object:
    """Return ``obj[key]``, raising ``ValueError`` (not ``KeyError``) if absent."""
    if key not in obj:
        raise ValueError(f"missing required field '{key}'")
    return obj[key]


def _parse_line(line: str) -> BackendEvent:
    """Parse one JSONL line into a BackendEvent.

    Raises:
        ValueError: if the ``type`` field is unknown or required keys are missing.
    """
    obj: dict[str, object] = json.loads(line)
    event_type = obj.get("type")

    if event_type == "text_chunk":
        return TextChunk(text=str(_require(obj, "text")))

    if event_type == "tool_use":
        raw_input = obj.get("input", {})
        # Ensure input is always dict[str, object]
        tool_input: dict[str, object] = (
            dict(raw_input) if isinstance(raw_input, dict) else {}
        )
        return ToolUseEvent(
            tool_id=str(_require(obj, "tool_id")),
            tool_name=str(_require(obj, "tool_name")),
            input=tool_input,
        )

    if event_type == "tool_result":
        return ToolResultEvent(
            tool_id=str(_require(obj, "tool_id")),
            output=str(_require(obj, "output")),
            is_error=bool(obj.get("is_error", False)),
        )

    if event_type == "turn_end":
        return TurnEnd(
            cost_usd=float(str(_require(obj, "cost_usd"))),
            input_tokens=int(str(_require(obj, "input_tokens"))),
            output_tokens=int(str(_require(obj, "output_tokens"))),
            context_pct=float(str(_require(obj, "context_pct"))),
        )

    if event_type == "error":
        return BackendError(
            message=str(_require(obj, "message")),
            retryable=bool(obj.get("retryable", False)),
        )

    raise ValueError(f"Unknown BackendEvent type: {event_type!r}")


def _load_transcript(path: Path) -> list[list[BackendEvent]]:
    """Load a JSONL transcript file and split into turns.

    Each turn is the sequence of events up to and including a
    ``TurnEnd`` or ``BackendError`` line.
    """
    turns: list[list[BackendEvent]] = []
    current_turn: list[BackendEvent] = []

    for lineno, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line:
            continue  # skip blank lines
        try:
            event = _parse_line(line)
        except ValueError as exc:
            raise ValueError(
                f"{path}:{lineno} — failed to parse JSONL line: {exc!r}\n"
                f"  line: {line!r}"
            ) from exc
        current_turn.append(event)
        if isinstance(event, (TurnEnd, BackendError)):
            turns.append(current_turn)
            current_turn = []

    # If the file ends without a TurnEnd (malformed but handled gracefully)
    if current_turn:
        turns.append(current_turn)

    return turns


# ---------------------------------------------------------------------------
# Per-session state
# ---------------------------------------------------------------------------


@dataclass
class _SessionState:
    """Internal state for a single mock session."""

    turns: list[list[BackendEvent]]        # all turns in the transcript
    turn_index: int = 0                    # index of the turn to replay next
    current_turn: list[BackendEvent] = field(default_factory=list)
    interrupted: bool = False


# ---------------------------------------------------------------------------
# MockBackend
# ---------------------------------------------------------------------------


class MockBackend:
    """Deterministic backend that replays a JSONL transcript for testing.

    Args:
        transcript: Pre-built list of turns (list[list[BackendEvent]]).
            Mutually exclusive with *transcript_path*.
        transcript_path: Path to a JSONL file to load.  Mutually exclusive
            with *transcript*.
        mock_summary: String returned by summarize(). Defaults to "[mock summary]".

    Either *transcript* or *transcript_path* must be provided.
    """

    def __init__(
        self,
        transcript: list[list[BackendEvent]] | None = None,
        transcript_path: str | Path | None = None,
        mock_summary: str = "[mock summary]",
    ) -> None:
        if transcript is not None and transcript_path is not None:
            raise ValueError("Provide transcript or transcript_path, not both.")
        if transcript is None and transcript_path is None:
            raise ValueError("One of transcript or transcript_path is required.")

        if transcript_path is not None:
            self._turns: list[list[BackendEvent]] = _load_transcript(
                Path(transcript_path)
            )
        else:
            # transcript is guaranteed non-None here (both-None check above)
            assert transcript is not None
            # Defensive copy so callers cannot mutate the shared list
            self._turns = [list(turn) for turn in transcript]

        self._mock_summary = mock_summary
        # session_ref -> _SessionState
        self._sessions: dict[str, _SessionState] = {}
        # Captures the messages argument from the most recent summarize() call.
        self.summarize_call_args: list[dict[str, object]] | None = None

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    async def start(self, session_ref: str | None = None) -> str:
        """Start or resume a session.

        Returns:
            A new ``mock-<uuid4>`` ref if *session_ref* is None, otherwise
            returns *session_ref* unchanged (resume semantics).
        """
        if session_ref is None:
            ref = f"mock-{uuid.uuid4()}"
        else:
            ref = session_ref

        if ref not in self._sessions:
            # Shallow copy: inner per-turn lists are copied in send()
            self._sessions[ref] = _SessionState(turns=list(self._turns))

        return ref

    async def send(self, session_ref: str, message: str) -> None:  # noqa: ARG002 — message ignored in mock
        """Advance to the next transcript turn.

        Idempotent when the transcript is exhausted: sets current turn to []
        so that *events()* yields nothing.
        """
        state = self._sessions[session_ref]
        state.interrupted = False  # reset interrupt flag for new turn

        if state.turn_index < len(state.turns):
            state.current_turn = list(state.turns[state.turn_index])
            state.turn_index += 1
        else:
            state.current_turn = []

    async def events(
        self, session_ref: str
    ) -> AsyncGenerator[BackendEvent, None]:
        """Async-yield each event in the current turn.

        If *interrupted* is True, yields nothing.
        """
        state = self._sessions[session_ref]
        for event in list(state.current_turn):  # snapshot to avoid aliasing
            if state.interrupted:
                return
            yield event

    async def interrupt(self, session_ref: str) -> None:
        """Mark session as interrupted; subsequent *events()* yields nothing."""
        state = self._sessions[session_ref]
        state.interrupted = True
        state.current_turn = []

    async def stop(self, session_ref: str) -> None:
        """Remove session state."""
        self._sessions.pop(session_ref, None)

    async def inject_tool_result(
        self,
        session_ref: str,  # noqa: ARG002
        tool_id: str,  # noqa: ARG002
        output: str,  # noqa: ARG002
        is_error: bool = False,  # noqa: ARG002
    ) -> None:
        """No-op: results are already embedded in the JSONL transcript."""
        return

    async def summarize(
        self,
        messages: list[dict[str, object]],
    ) -> str:
        """Return the configured mock summary string (no network call).

        Also records *messages* in ``summarize_call_args`` so tests can assert
        the correct conversation history was passed.
        """
        self.summarize_call_args = copy.deepcopy(list(messages))
        return self._mock_summary
