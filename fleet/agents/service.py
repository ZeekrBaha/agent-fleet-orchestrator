"""AgentService — orchestrates AgentSessions and InboxService (Task 2.2).

Public API:
    AgentService(db, event_service, inbox_service)
    AgentService.create_agent(scope, name, role, backend, model, ...) -> AgentRecord
    AgentService.send_message(agent_id, sender, message) -> int
    AgentService.interrupt_agent(agent_id) -> None
    AgentService.get_agent(agent_id) -> AgentRecord | None
    AgentService.list_agents(scope) -> list[AgentRecord]
    AgentService.archive_agent(agent_id) -> None
    AgentService.restore_sessions(backends) -> None
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection, text

from fleet.agents.backends.protocol import AgentBackend
from fleet.agents.budget import BudgetEnforcer
from fleet.agents.inbox import InboxService
from fleet.agents.promptbuild import MissingRolePromptError, assemble_prompt
from fleet.agents.session import AgentSession
from fleet.db import DatabaseManager
from fleet.events.service import EventService
from fleet.models import AgentRecord

if TYPE_CHECKING:
    from fleet.approvals.service import ApprovalService
    from fleet.memory.service import MemoryService

_logger = logging.getLogger(__name__)


class AgentService:
    """Orchestrates per-agent sessions and the shared inbox."""

    def __init__(
        self,
        db: DatabaseManager,
        event_service: EventService,
        inbox_service: InboxService,
        *,
        memory_svc: MemoryService | None = None,
        approval_svc: ApprovalService | None = None,
    ) -> None:
        self._db = db
        self._event_service = event_service
        self._inbox = inbox_service
        self._memory_svc = memory_svc
        self._approval_svc = approval_svc
        # agent_id -> AgentSession
        self._sessions: dict[str, AgentSession] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_agent(
        self,
        scope: str,
        name: str,
        role: str,
        backend: AgentBackend,
        model: str,
        *,
        parent_id: str | None = None,
        repository_id: str | None = None,
        budget_soft_usd: float | None = None,
        budget_hard_usd: float | None = None,
        backend_name: str = "mock",
        task_description: str = "",
    ) -> AgentRecord:
        """Insert agent row (status=idle), emit state_change, start session task.

        Calls assemble_prompt() to build the system prompt before creating the
        agent.  If the role prompt file is missing, MissingRolePromptError is
        raised (ADR-005: fail-closed — never silently fall back to a base prompt).
        An error event is emitted and the agent record is inserted with
        status=failed in that case.
        """
        agent_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        # -- Build system prompt (ADR-005: fail on missing role prompt) ----------
        # assemble_prompt validates that the role prompt file exists.
        # The returned system_prompt will be passed to real backends in a later
        # phase; for MockBackend (MVP) it is assembled here for validation only.
        try:
            assemble_prompt(
                role=role,
                task_prompt=task_description,
                team_state="",
                memory_snippets=[],
                tool_descriptions=[],
                workspace_context="",
            )
        except MissingRolePromptError:
            # Emit error event, insert a failed agent row, then re-raise so the
            # caller knows the agent was not started (ADR-005: no silent fallback).
            await self._event_service.append(
                scope,
                "error",
                f"Missing role prompt for role={role!r}; agent creation failed",
                agent_id=agent_id,
                payload={"role": role, "error": "MissingRolePromptError"},
            )

            def _write_failed(conn: Connection) -> None:
                conn.execute(
                    text(
                        "INSERT INTO agents"
                        " (id, name, scope, role, backend, model, status,"
                        "  parent_id, repository_id, budget_soft_usd, budget_hard_usd,"
                        "  created_at, updated_at)"
                        " VALUES"
                        " (:id, :name, :scope, :role, :backend, :model, 'failed',"
                        "  :parent_id, :repository_id,"
                        "  :budget_soft_usd, :budget_hard_usd,"
                        "  :created_at, :updated_at)"
                    ),
                    {
                        "id": agent_id,
                        "name": name,
                        "scope": scope,
                        "role": role,
                        "backend": backend_name,
                        "model": model,
                        "parent_id": parent_id,
                        "repository_id": repository_id,
                        "budget_soft_usd": budget_soft_usd,
                        "budget_hard_usd": budget_hard_usd,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                conn.commit()

            await self._db.write(_write_failed)
            raise

        def _write(conn: Connection) -> None:
            conn.execute(
                text(
                    "INSERT INTO agents"
                    " (id, name, scope, role, backend, model, status,"
                    "  parent_id, repository_id, budget_soft_usd, budget_hard_usd,"
                    "  created_at, updated_at)"
                    " VALUES"
                    " (:id, :name, :scope, :role, :backend, :model, 'idle',"
                    "  :parent_id, :repository_id, :budget_soft_usd, :budget_hard_usd,"
                    "  :created_at, :updated_at)"
                ),
                {
                    "id": agent_id,
                    "name": name,
                    "scope": scope,
                    "role": role,
                    "backend": backend_name,
                    "model": model,
                    "parent_id": parent_id,
                    "repository_id": repository_id,
                    "budget_soft_usd": budget_soft_usd,
                    "budget_hard_usd": budget_hard_usd,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            conn.commit()

        await self._db.write(_write)

        # Emit initial state_change event
        await self._event_service.append(
            scope,
            "state_change",
            f"Agent {name} created (idle)",
            agent_id=agent_id,
            payload={"status": "idle"},
        )

        # Start session task
        self._start_session(
            agent_id, scope, backend, role=role, task_description=task_description
        )

        record = await self.get_agent(agent_id)
        if record is None:
            raise RuntimeError(f"agent row missing after insert: {agent_id}")
        return record

    async def send_message(self, agent_id: str, sender: str, message: str) -> int:
        """Enqueue an inbox message. Returns inbox id."""
        return await self._inbox.enqueue(agent_id, sender, message)

    async def interrupt_agent(self, agent_id: str) -> None:
        """Interrupt the running turn for agent_id (if any)."""
        session = self._sessions.get(agent_id)
        if session is not None:
            await session.interrupt()

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        """Fetch agent row from DB. Returns None if not found."""
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT id, name, scope, role, backend, model, status,"
                    " parent_id, repository_id, session_ref, worktree_id,"
                    " context_pct, cost_usd, budget_soft_usd, budget_hard_usd,"
                    " created_at, updated_at"
                    " FROM agents WHERE id = :id"
                ),
                {"id": agent_id},
            ).fetchone()

        if row is None:
            return None
        return _row_to_record(row)

    async def list_agents(self, scope: str) -> list[AgentRecord]:
        """List all non-archived agents in scope."""
        with self._db.read_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, name, scope, role, backend, model, status,"
                    " parent_id, repository_id, session_ref, worktree_id,"
                    " context_pct, cost_usd, budget_soft_usd, budget_hard_usd,"
                    " created_at, updated_at"
                    " FROM agents"
                    " WHERE scope = :scope AND status != 'archived'"
                    " ORDER BY created_at ASC"
                ),
                {"scope": scope},
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    async def set_worktree_id(self, agent_id: str, worktree_id: str) -> None:
        """Set the worktree_id column for an existing agent row."""
        now = datetime.now(UTC).isoformat()

        def _write(conn: Connection) -> None:
            conn.execute(
                text(
                    "UPDATE agents"
                    " SET worktree_id = :worktree_id, updated_at = :now"
                    " WHERE id = :id"
                ),
                {"worktree_id": worktree_id, "now": now, "id": agent_id},
            )
            conn.commit()

        await self._db.write(_write)

    async def archive_agent(self, agent_id: str) -> None:
        """Set status=archived, emit state_change, stop session.

        Order matters (P1-21): cancel the session task FIRST to prevent queued
        _set_status calls from resurrecting the agent after we mark it archived.
        """
        # Cancel the session task BEFORE writing to DB (P1-21 fix).
        # If we archived first, a still-running session could write a new status
        # (e.g. "idle") on its next _set_status call, overwriting "archived".
        await self._stop_session(agent_id)

        now = datetime.now(UTC).isoformat()

        def _write(conn: Connection) -> None:
            conn.execute(
                text(
                    "UPDATE agents SET status = 'archived', updated_at = :now"
                    " WHERE id = :id"
                ),
                {"now": now, "id": agent_id},
            )
            conn.commit()

        await self._db.write(_write)
        # Fetch scope via the async read path instead of the sync helper.
        archived_record = await self.get_agent(agent_id)
        scope = archived_record.scope if archived_record is not None else "unknown"
        await self._event_service.append(
            scope,
            "state_change",
            f"Agent {agent_id} archived",
            agent_id=agent_id,
            payload={"status": "archived"},
        )

    async def restore_sessions(
        self,
        backends: dict[str, AgentBackend] | None = None,
    ) -> None:
        """On startup: reload all non-archived agents and restart their sessions.

        Args:
            backends: Optional mapping of agent_id -> backend instance.
                      If not provided for a given agent_id, a MockBackend
                      with an empty transcript is used as a placeholder.

        For each agent, loads the most recent compaction memory (if any) and
        passes its content as prior_context to the new session so the agent
        resumes with its prior summary rather than a blank slate.
        """
        from fleet.agents.backends.mock import MockBackend

        backends = backends or {}

        with self._db.read_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, name, scope, role, backend, model, status,"
                    " parent_id, repository_id, session_ref, worktree_id,"
                    " context_pct, cost_usd, budget_soft_usd, budget_hard_usd,"
                    " created_at, updated_at"
                    " FROM agents WHERE status != 'archived'"
                )
            ).fetchall()

        for row in rows:
            agent_id = row.id
            scope = row.scope
            if agent_id not in self._sessions:
                backend = backends.get(agent_id, MockBackend(transcript=[]))
                prior_context = ""
                if self._memory_svc is not None:
                    recent = await self._memory_svc.read_recent(
                        agent_id, scope, kind="compaction", limit=1
                    )
                    if recent:
                        prior_context = recent[-1].content
                self._start_session(
                    agent_id, scope, backend, prior_context=prior_context
                )

    async def stop_all(self) -> None:
        """Cancel all active session tasks and wait for them to finish.

        Called during graceful shutdown so sessions drain before the DB
        connection is closed.
        """
        sessions = list(self._sessions.values())
        # Cancel all first
        for s in sessions:
            if s._task and not s._task.done():
                s._task.cancel()
        # Then await all
        for s in sessions:
            if s._task:
                try:
                    await s._task
                except asyncio.CancelledError:
                    # Normal cancellation
                    pass
                except Exception as exc:
                    _logger.warning("session task failed on stop: %s", exc)
        self._sessions.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_session(
        self,
        agent_id: str,
        scope: str,
        backend: AgentBackend,
        prior_context: str = "",
        role: str = "worker",
        task_description: str = "",
    ) -> None:
        """Create an AgentSession and launch it as an asyncio.Task."""
        budget = BudgetEnforcer(
            agent_id=agent_id,
            scope=scope,
            db=self._db,
            event_service=self._event_service,
        )
        session = AgentSession(
            agent_id=agent_id,
            scope=scope,
            backend=backend,
            event_service=self._event_service,
            inbox=self._inbox,
            db=self._db,
            budget=budget,
            memory_svc=self._memory_svc,
            prior_context=prior_context,
            role=role,
            task_description=task_description,
            approval_svc=self._approval_svc,
        )
        task = asyncio.create_task(session.run(), name=f"session:{agent_id}")
        # P1-23: add done-callback so silent task death is logged as an error.
        task.add_done_callback(
            functools.partial(_session_done_callback, agent_id=agent_id)
        )
        session._task = task
        self._sessions[agent_id] = session

    async def _stop_session(self, agent_id: str) -> None:
        """Cancel the session task for agent_id (if running)."""
        session = self._sessions.get(agent_id)
        if session is None:
            return
        task = session._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # Cancelled or completed with an error — both are fine on shutdown
                pass
        self._sessions.pop(agent_id, None)


# ---------------------------------------------------------------------------
# Module-level helpers (sync, no DB write queue needed)
# ---------------------------------------------------------------------------


def _session_done_callback(task: asyncio.Task[None], *, agent_id: str) -> None:
    """Done-callback for session tasks (P1-23).

    If the task completed with an unhandled exception (e.g. backend.start()
    raised), log an error so the failure is visible rather than silently lost.
    Cancellation is normal shutdown and is not logged as an error.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _logger.error(
            "session task for agent %s died: %s",
            agent_id,
            exc,
            exc_info=exc,
        )


def _row_to_record(row) -> AgentRecord:  # type: ignore[no-untyped-def]
    """Convert a SQLAlchemy row to an AgentRecord."""
    return AgentRecord(
        id=row.id,
        name=row.name,
        scope=row.scope,
        role=row.role,
        backend=row.backend,
        model=row.model,
        status=row.status,
        parent_id=row.parent_id,
        repository_id=row.repository_id,
        session_ref=row.session_ref,
        worktree_id=row.worktree_id,
        context_pct=row.context_pct,
        cost_usd=row.cost_usd,
        budget_soft_usd=row.budget_soft_usd,
        budget_hard_usd=row.budget_hard_usd,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


