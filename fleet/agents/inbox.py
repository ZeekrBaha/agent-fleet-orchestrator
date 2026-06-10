"""InboxService — per-agent FIFO message queue backed by SQLite (Task 2.2).

At-least-once delivery: pending rows survive process restarts.
FIFO per agent: ORDER BY id ASC.
Notification: asyncio.Event per agent_id to wake a waiting AgentSession.

Public API:
    InboxService(db)
    InboxService.enqueue(to_agent_id, sender, message) -> int
    InboxService.deliver_next(agent_id) -> InboxMessage | None
    InboxService.mark_delivered(inbox_id) -> None
    InboxService.mark_failed(inbox_id) -> None
    InboxService.pending_count(agent_id) -> int
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from fleet.db import DatabaseManager
from fleet.models import InboxMessage


class InboxService:
    """FIFO inbox backed by the SQLite `inbox` table."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db
        # Per-agent asyncio.Event to wake a waiting AgentSession.
        # Lazily created on first access.
        self._events: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Notification helpers
    # ------------------------------------------------------------------

    def _get_event(self, agent_id: str) -> asyncio.Event:
        """Return (or create) the asyncio.Event for agent_id."""
        if agent_id not in self._events:
            self._events[agent_id] = asyncio.Event()
        return self._events[agent_id]

    def notify(self, agent_id: str) -> None:
        """Set the asyncio.Event for agent_id (wakes a waiting session)."""
        self._get_event(agent_id).set()

    async def wait_for_message(self, agent_id: str) -> None:
        """Block until a message is enqueued for agent_id, then clear the event."""
        ev = self._get_event(agent_id)
        # No race between wait() return and clear(): asyncio is single-threaded
        # and no await occurs between the two calls, so no other coroutine can
        # set the event again before clear() runs.
        await ev.wait()
        ev.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enqueue(self, to_agent_id: str, sender: str, message: str) -> int:
        """Insert an inbox row (status=pending) and wake waiting session.

        Returns:
            The auto-assigned inbox row id.
        """
        now = datetime.now(UTC).isoformat()

        def _write(conn: Connection) -> int:
            result = conn.execute(
                text(
                    "INSERT INTO inbox"
                    " (to_agent_id, sender, message, status, created_at)"
                    " VALUES"
                    " (:to_agent_id, :sender, :message, 'pending', :created_at)"
                ),
                {
                    "to_agent_id": to_agent_id,
                    "sender": sender,
                    "message": message,
                    "created_at": now,
                },
            )
            conn.commit()
            # lastrowid is int after a successful INSERT; assert to narrow type
            assert result.lastrowid is not None
            return result.lastrowid

        row_id: int = await self._db.write(_write)
        self.notify(to_agent_id)
        return row_id

    async def deliver_next(self, agent_id: str) -> InboxMessage | None:
        """Return the oldest pending inbox message for agent_id, or None."""
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT id, to_agent_id, sender, message,"
                    " status, created_at, delivered_at"
                    " FROM inbox"
                    " WHERE to_agent_id = :agent_id AND status = 'pending'"
                    " ORDER BY id ASC"
                    " LIMIT 1"
                ),
                {"agent_id": agent_id},
            ).fetchone()

        if row is None:
            return None

        return InboxMessage(
            id=row.id,
            to_agent_id=row.to_agent_id,
            sender=row.sender,
            message=row.message,
            status=row.status,
            created_at=row.created_at,
            delivered_at=row.delivered_at,
        )

    async def mark_delivered(self, inbox_id: int) -> None:
        """Set status=delivered and delivered_at=now for inbox_id."""
        now = datetime.now(UTC).isoformat()

        def _write(conn: Connection) -> None:
            conn.execute(
                text(
                    "UPDATE inbox SET status = 'delivered', delivered_at = :now"
                    " WHERE id = :id"
                ),
                {"now": now, "id": inbox_id},
            )
            conn.commit()

        await self._db.write(_write)

    async def mark_failed(self, inbox_id: int) -> None:
        """Set status=failed for inbox_id."""

        def _write(conn: Connection) -> None:
            conn.execute(
                text("UPDATE inbox SET status = 'failed' WHERE id = :id"),
                {"id": inbox_id},
            )
            conn.commit()

        await self._db.write(_write)

    async def pending_count(self, agent_id: str) -> int:
        """Count pending messages for agent_id."""
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM inbox"
                    " WHERE to_agent_id = :agent_id AND status = 'pending'"
                ),
                {"agent_id": agent_id},
            ).fetchone()
        return row[0] if row else 0
