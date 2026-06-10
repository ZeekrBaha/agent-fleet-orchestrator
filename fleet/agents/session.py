"""AgentSession — per-agent async turn loop (Task 2.2).

One asyncio.Task per agent. Manages the turn loop:
  1. Wait for inbox messages (or hibernate after idle timeout)
  2. On message: set status → running, emit state_change
  3. Drive backend turn, stream events, emit fleet events
  4. On TurnEnd: emit agent_message + state_change(idle), mark delivered
  5. On BackendError: retry once if retryable, else emit error + set failed
  6. Turn timeout: interrupt + emit error event
  7. Hibernate: after idle_hibernate_s, emit state_change(waiting)

Public API:
    AgentSession(agent_id, backend, event_service, db, ...)
    AgentSession.run()       — start the main loop (called as asyncio.Task)
    AgentSession.interrupt() — interrupt current turn gracefully
    AgentSession.status      — current AgentStatus
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from fleet.agents.backends.protocol import (
    AgentBackend,
    BackendError,
    TextChunk,
    ToolResultEvent,
    ToolUseEvent,
    TurnEnd,
)
from fleet.agents.inbox import InboxService
from fleet.db import DatabaseManager
from fleet.events.service import EventService
from fleet.models import AgentStatus


class AgentSession:
    """Manages the async turn loop for a single agent."""

    def __init__(
        self,
        agent_id: str,
        backend: AgentBackend,
        event_service: EventService,
        db: DatabaseManager,
        inbox: InboxService,
        scope: str,
        *,
        turn_timeout_s: float = 300.0,
        idle_hibernate_s: float = 3600.0,
    ) -> None:
        self._agent_id = agent_id
        self._backend = backend
        self._event_service = event_service
        self._db = db
        self._inbox = inbox
        self._scope = scope
        self._turn_timeout_s = turn_timeout_s
        self._idle_hibernate_s = idle_hibernate_s

        self._status: AgentStatus = "idle"
        self._session_ref: str | None = None
        self._interrupt_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> AgentStatus:
        return self._status

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def interrupt(self) -> None:
        """Signal an interrupt to the current turn."""
        self._interrupt_event.set()
        if self._session_ref is not None:
            await self._backend.interrupt(self._session_ref)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _set_status(self, new_status: AgentStatus) -> None:
        """Update the agents table and emit a state_change event."""
        self._status = new_status
        now = datetime.now(UTC).isoformat()

        def _write(conn: Connection) -> None:
            conn.execute(
                text(
                    "UPDATE agents SET status = :status, updated_at = :now"
                    " WHERE id = :id"
                ),
                {"status": new_status, "now": now, "id": self._agent_id},
            )
            conn.commit()

        # Two separate writes: DB row update then event append. The small
        # window between them is intentional — each is independently durable.
        await self._db.write(_write)
        await self._event_service.append(
            self._scope,
            "state_change",
            f"Agent {self._agent_id} → {new_status}",
            agent_id=self._agent_id,
            payload={"status": new_status},
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: wait for inbox → run turns → repeat."""
        # Start (or resume) the backend session
        self._session_ref = await self._backend.start(self._session_ref)

        # Pick up any pre-existing pending messages (at-least-once delivery)
        while True:
            # Check for pre-existing messages before waiting
            msg = await self._inbox.deliver_next(self._agent_id)
            if msg is None:
                # Wait for next message or hibernate timeout
                try:
                    await asyncio.wait_for(
                        self._inbox.wait_for_message(self._agent_id),
                        timeout=self._idle_hibernate_s,
                    )
                except TimeoutError:
                    # Hibernate: emit waiting state
                    await self._set_status("waiting")
                    # Resume on next message (loop back to wait)
                    continue

                # After waking from notification, check for a message
                msg = await self._inbox.deliver_next(self._agent_id)
                if msg is None:
                    # Spurious wake-up — loop back
                    continue

            # We have a message — run a turn
            assert msg.id is not None
            inbox_id: int = msg.id
            self._interrupt_event.clear()

            await self._set_status("running")

            try:
                await asyncio.wait_for(
                    self._run_turn(msg.message, inbox_id),
                    timeout=self._turn_timeout_s,
                )
            except TimeoutError:
                await self._backend.interrupt(self._session_ref)
                await self._event_service.append(
                    self._scope,
                    "error",
                    f"Turn timed out after {self._turn_timeout_s}s",
                    agent_id=self._agent_id,
                    payload={"reason": "turn_timeout", "inbox_id": inbox_id},
                )
                await self._inbox.mark_failed(inbox_id)
                await self._set_status("idle")
            except asyncio.CancelledError:
                # Session is being shut down
                return

    async def _run_turn(self, message: str, inbox_id: int) -> None:
        """Execute one agent turn: send message, stream events, finalize."""
        assert self._session_ref is not None

        try:
            await self._backend.send(self._session_ref, message)
        except Exception as exc:  # noqa: BLE001 — backend.send can raise any provider error; map to failed state
            await self._event_service.append(
                self._scope, "error", f"backend.send failed: {exc}",
                agent_id=self._agent_id, payload={"exc": str(exc)}
            )
            await self._inbox.mark_failed(inbox_id)
            await self._set_status("failed")
            return

        result = await self._drain_events(message, inbox_id, "", allow_retry=True)

        if result == "retry":
            try:
                await self._backend.send(self._session_ref, message)
            except Exception as exc:  # noqa: BLE001 — backend.send can raise any provider error; map to failed state
                await self._event_service.append(
                    self._scope, "error", f"backend.send failed: {exc}",
                    agent_id=self._agent_id, payload={"exc": str(exc)}
                )
                await self._inbox.mark_failed(inbox_id)
                await self._set_status("failed")
                return

            await self._drain_events(message, inbox_id, "", allow_retry=False)

    async def _drain_events(
        self,
        message: str,
        inbox_id: int,
        accumulated_text: str,
        *,
        allow_retry: bool,
    ) -> str:
        """Drain the backend event stream for one turn attempt.

        Handles all event types (TextChunk, ToolUseEvent, ToolResultEvent,
        TurnEnd, BackendError) and finalises inbox + status for terminal outcomes.

        Returns:
            "done"        — TurnEnd reached or generator exhausted; idle.
            "retry"       — Retryable BackendError; caller re-sends and calls again.
            "interrupted" — Interrupt received; message marked failed, state idle.
            "failed"      — Non-retryable error; message failed, state failed.
        """
        assert self._session_ref is not None

        async for event in self._backend.events(self._session_ref):
            # Check for interrupt signal between events
            if self._interrupt_event.is_set():
                await self._backend.interrupt(self._session_ref)
                await self._inbox.mark_failed(inbox_id)
                await self._set_status("idle")
                return "interrupted"

            if isinstance(event, TextChunk):
                accumulated_text += event.text

            elif isinstance(event, ToolUseEvent):
                await self._event_service.append(
                    self._scope,
                    "tool_call",
                    f"Tool call: {event.tool_name}",
                    agent_id=self._agent_id,
                    payload={
                        "tool_id": event.tool_id,
                        "tool_name": event.tool_name,
                        "input": event.input,
                    },
                )

            elif isinstance(event, ToolResultEvent):
                await self._event_service.append(
                    self._scope,
                    "tool_result",
                    f"Tool result for: {event.tool_id}",
                    agent_id=self._agent_id,
                    payload={
                        "tool_id": event.tool_id,
                        "output": event.output,
                        "is_error": event.is_error,
                    },
                )

            elif isinstance(event, TurnEnd):
                # Emit agent_message with accumulated text
                await self._event_service.append(
                    self._scope,
                    "agent_message",
                    accumulated_text or "(no text)",
                    agent_id=self._agent_id,
                    payload={
                        "text": accumulated_text,
                        "cost_usd": event.cost_usd,
                        "input_tokens": event.input_tokens,
                        "output_tokens": event.output_tokens,
                        "context_pct": event.context_pct,
                    },
                )
                await self._inbox.mark_delivered(inbox_id)
                await self._set_status("idle")
                return "done"

            elif isinstance(event, BackendError):
                if event.retryable and allow_retry:
                    # Signal caller to retry once
                    return "retry"
                else:
                    error_label = (
                        "Backend error after retry"
                        if not allow_retry
                        else "Backend error"
                    )
                    await self._event_service.append(
                        self._scope,
                        "error",
                        f"{error_label}: {event.message}",
                        agent_id=self._agent_id,
                        payload={
                            "message": event.message,
                            "retryable": event.retryable,
                            "inbox_id": inbox_id,
                        },
                    )
                    await self._inbox.mark_failed(inbox_id)
                    await self._set_status("failed")
                    return "failed"

        # Generator exhausted without TurnEnd — treat as completed turn
        # (e.g. empty transcript in mock)
        await self._inbox.mark_delivered(inbox_id)
        await self._set_status("idle")
        return "done"
