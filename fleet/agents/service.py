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
import uuid
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from fleet.agents.backends.protocol import AgentBackend
from fleet.agents.budget import BudgetEnforcer
from fleet.agents.inbox import InboxService
from fleet.agents.session import AgentSession
from fleet.db import DatabaseManager
from fleet.events.service import EventService
from fleet.models import AgentRecord


class AgentService:
    """Orchestrates per-agent sessions and the shared inbox."""

    def __init__(
        self,
        db: DatabaseManager,
        event_service: EventService,
        inbox_service: InboxService,
    ) -> None:
        self._db = db
        self._event_service = event_service
        self._inbox = inbox_service
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
    ) -> AgentRecord:
        """Insert agent row (status=idle), emit state_change, start session task."""
        agent_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

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
        self._start_session(agent_id, scope, backend)

        record = await self.get_agent(agent_id)
        assert record is not None
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

    async def archive_agent(self, agent_id: str) -> None:
        """Set status=archived, emit state_change, stop session."""
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
        await self._event_service.append(
            # Fetch scope from DB first
            _get_scope_sync(self._db, agent_id),
            "state_change",
            f"Agent {agent_id} archived",
            agent_id=agent_id,
            payload={"status": "archived"},
        )
        await self._stop_session(agent_id)

    async def restore_sessions(
        self,
        backends: dict[str, AgentBackend] | None = None,
    ) -> None:
        """On startup: reload all non-archived agents and restart their sessions.

        Args:
            backends: Optional mapping of agent_id -> backend instance.
                      If not provided for a given agent_id, a MockBackend
                      with an empty transcript is used as a placeholder.
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
                self._start_session(agent_id, scope, backend)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_session(
        self, agent_id: str, scope: str, backend: AgentBackend
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
        )
        task = asyncio.create_task(session.run(), name=f"session:{agent_id}")
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


def _get_scope_sync(db: DatabaseManager, agent_id: str) -> str:
    """Synchronously read the scope for agent_id (used during archive)."""
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT scope FROM agents WHERE id = :id"),
            {"id": agent_id},
        ).fetchone()
    return row.scope if row else "unknown"
