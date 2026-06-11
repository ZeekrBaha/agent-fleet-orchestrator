"""AgentSession — per-agent async turn loop (Task 2.2).

One asyncio.Task per agent. Manages the turn loop:
  1. Wait for inbox messages (or hibernate after idle timeout)
  2. On message: set status → running, emit state_change
  3. Drive backend turn, stream events, emit fleet events
  4. On TurnEnd: emit agent_message + state_change(idle), mark delivered
  5. On BackendError: retry once if retryable, else emit error + set failed
  6. Turn timeout: interrupt + emit error event
  7. Hibernate: after idle_hibernate_s, emit state_change(waiting)
  8. Budget enforcement: after each TurnEnd, BudgetEnforcer.record_turn_cost()
     is called; PAUSE result → status=paused_budget, wait for resume()

Public API:
    AgentSession(agent_id, scope, backend, event_service, inbox, db, budget, ...)
    AgentSession.run()       — start the main loop (called as asyncio.Task)
    AgentSession.interrupt() — interrupt current turn gracefully
    AgentSession.resume()    — resume after a budget approval is granted
    AgentSession.status      — current AgentStatus
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection, text

from fleet.agents.backends.protocol import (
    AgentBackend,
    BackendError,
    TextChunk,
    ToolResultEvent,
    ToolUseEvent,
    TurnEnd,
)
from fleet.agents.budget import BudgetAction, BudgetEnforcer
from fleet.agents.inbox import InboxService
from fleet.agents.promptbuild import assemble_prompt
from fleet.approvals.service import ApprovalTimeoutError
from fleet.db import DatabaseManager
from fleet.events.service import EventService
from fleet.models import AgentStatus

if TYPE_CHECKING:
    from fleet.approvals.service import ApprovalService
    from fleet.memory.service import MemoryService

_DEFAULT_COMPACTION_THRESHOLD = 80_000


class AgentSession:
    """Manages the async turn loop for a single agent."""

    def __init__(
        self,
        agent_id: str,
        scope: str,
        backend: AgentBackend,
        event_service: EventService,
        inbox: InboxService,
        db: DatabaseManager,
        budget: BudgetEnforcer,
        *,
        turn_timeout_s: float = 300.0,
        idle_hibernate_s: float = 3600.0,
        memory_svc: MemoryService | None = None,
        compaction_threshold: int = _DEFAULT_COMPACTION_THRESHOLD,
        prior_context: str = "",
        role: str = "worker",
        task_description: str = "",
        approval_svc: ApprovalService | None = None,
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
        self._resume_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

        self._role = role
        self._budget = budget
        self._approval_svc = approval_svc
        self._memory_svc = memory_svc
        self._compaction_threshold = compaction_threshold
        self._prior_context = prior_context
        # Cumulative token counter across turns; reset after each compaction
        self._cumulative_tokens: int = 0
        # Conversation history accumulated across turns for compaction summarization
        self._conversation_history: list[dict[str, object]] = []
        # Assembled system prompt (built from role + prior_context); stored so
        # tests and future real backends can inspect/use the full prompt text.
        try:
            assembled = assemble_prompt(
                role=role,
                task_prompt=task_description,
                prior_context=prior_context,
            )
            self._system_prompt: str = assembled.system_prompt
        except Exception:  # noqa: BLE001 — if prompt assembly fails, degrade gracefully
            self._system_prompt = prior_context

    @property
    def status(self) -> AgentStatus:
        return self._status

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def interrupt(self) -> None:
        """Signal an interrupt to the current turn."""
        self._interrupt_event.set()
        self._resume_event.set()  # unblock _wait_for_resume if paused_budget
        if self._session_ref is not None:
            await self._backend.interrupt(self._session_ref)

    async def resume(self) -> None:
        """Resume the session after a budget approval is granted."""
        self._resume_event.set()

    async def _wait_for_resume(self) -> None:
        """Block until resume() is called (budget approval received)."""
        self._resume_event.clear()
        await self._resume_event.wait()

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
        # Start (or resume) the backend session.
        # Wrap in try/except (P1-23): if start() raises, the exception propagates
        # out of this coroutine and is caught by the done-callback in _start_session,
        # which logs it as an error rather than letting it vanish silently.
        try:
            self._session_ref = await self._backend.start(self._session_ref)
        except Exception as exc:  # noqa: BLE001
            await self._event_service.append(
                self._scope,
                "error",
                f"backend.start failed for agent {self._agent_id}: {exc}",
                agent_id=self._agent_id,
                payload={"exc": str(exc), "reason": "backend_start_failed"},
            )
            raise

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
                    # Emit only on first transition into waiting, not every cycle.
                    if self._status != "waiting":
                        await self._set_status("waiting")
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

        # Pre-turn budget gate: if already over limit, pause without calling API.
        pre_action = await self._budget.check_pre_turn()
        if pre_action == BudgetAction.PAUSE:
            await self._set_status("paused_budget")
            await self._wait_for_resume()
            if self._interrupt_event.is_set():
                await self._set_status("idle")
            return

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

    async def _compact(self, tokens_before: int) -> None:
        """Summarise the current conversation and write a compaction memory row.

        Called by _drain_events when cumulative tokens exceed the threshold.
        Emits a state_change("context_compacted") event with tokens_before and
        memory_id in the payload.
        """
        assert self._memory_svc is not None

        # Pass the accumulated conversation history so the backend can produce
        # a meaningful summary rather than summarizing an empty list.
        messages = list(self._conversation_history)
        summary = await self._backend.summarize(messages)
        # Clear history after compaction so it doesn't grow unbounded.
        self._conversation_history.clear()
        # Reset the backend's in-memory message list so it doesn't keep growing.
        if self._session_ref is not None:
            await self._backend.reset_history(self._session_ref, summary)

        memory_id = await self._memory_svc.write(
            agent_id=self._agent_id,
            scope=self._scope,
            kind="compaction",
            content=summary,
            metadata={"tokens_before": tokens_before},
        )

        await self._event_service.append(
            self._scope,
            "state_change",
            "context_compacted",
            agent_id=self._agent_id,
            payload={"tokens_before": tokens_before, "memory_id": memory_id},
        )

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

        # Record the user turn in conversation history for compaction summaries.
        if message:
            self._conversation_history.append(
                {"role": "user", "content": message}
            )

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
                # Record the assistant turn in conversation history.
                if accumulated_text:
                    self._conversation_history.append(
                        {"role": "assistant", "content": accumulated_text}
                    )

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

                # Accumulate tokens and trigger compaction if threshold exceeded.
                self._cumulative_tokens += event.input_tokens + event.output_tokens
                if (
                    self._memory_svc is not None
                    and self._cumulative_tokens >= self._compaction_threshold
                ):
                    await self._compact(tokens_before=self._cumulative_tokens)
                    self._cumulative_tokens = 0

                # Budget enforcement: check limits after accumulating this turn's cost.
                budget_action = await self._budget.record_turn_cost(event)
                if budget_action == BudgetAction.PAUSE:
                    # Hard budget exceeded — request approval and pause until decided.
                    await self._set_status("paused_budget")

                    if self._approval_svc is not None:
                        approval_id = await self._approval_svc.request(
                            self._scope,
                            self._agent_id,
                            "budget_exceeded",
                            f"Agent {self._agent_id} exceeded hard budget limit",
                        )
                        try:
                            decision = await self._approval_svc.wait_for_decision(
                                approval_id
                            )
                        except asyncio.CancelledError:
                            # Session is being shut down — propagate.
                            raise
                        except ApprovalTimeoutError:
                            # Timed out — stay paused; resume() resumes later
                            await self._set_status("paused_budget")
                            return "interrupted"
                        if self._interrupt_event.is_set():
                            await self._set_status("idle")
                            return "interrupted"
                        if decision == "approve":
                            await self._set_status("running")
                            await self._event_service.append(
                                self._scope,
                                "state_change",
                                f"Agent {self._agent_id} budget approved",
                                agent_id=self._agent_id,
                                payload={"status": "budget_approved"},
                            )
                        else:
                            await self._event_service.append(
                                self._scope,
                                "state_change",
                                f"Agent {self._agent_id} budget denied",
                                agent_id=self._agent_id,
                                payload={"status": "budget_denied"},
                            )
                            # Stay paused; session will not proceed to idle.
                            return "done"
                    else:
                        # No approval service — fall back to bare event-based wait.
                        await self._wait_for_resume()
                        if self._interrupt_event.is_set():
                            await self._set_status("idle")
                            return "interrupted"

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
