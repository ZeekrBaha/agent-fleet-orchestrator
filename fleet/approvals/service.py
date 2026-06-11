"""ApprovalService — request, wait for, and decide on human-gated operations.

Public API:
    ApprovalTimeoutError    — raised by wait_for_decision() on timeout
    ApprovalService         — manages approval rows and asyncio waiters
        .request(scope, agent_id, action, description, *, metadata=None) -> str
        .decide(approval_id, decision, comment="") -> ApprovalRecord
        .wait_for_decision(approval_id, *, timeout_s=300.0) -> Literal["approve","deny"]
        .get(approval_id) -> ApprovalRecord | None
        .list_pending(scope) -> list[ApprovalRecord]
        .load_pending() -> None   (called at startup to rebuild waiter Events)
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import Connection, text

from fleet.db import DatabaseManager
from fleet.events.service import EventService
from fleet.models import ApprovalRecord


class ApprovalTimeoutError(Exception):
    """Raised when wait_for_decision() expires before a human decides."""


class ApprovalService:
    """Manages the approval queue: create rows, block callers, wake on decision."""

    def __init__(self, db: DatabaseManager, event_service: EventService) -> None:
        self._db = db
        self._event_service = event_service
        # Per-approval asyncio.Event, keyed by approval_id string.
        # Created on request(); set by decide(); waited on by wait_for_decision().
        self._waiters: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def load_pending(self) -> None:
        """Load all pending approvals from DB and create waiter Events for them.

        Must be called once at startup so that decide() can wake callers that
        are waiting for rows that were pending before this process started.
        """
        with self._db.read_connection() as conn:
            rows = conn.execute(
                text("SELECT id FROM approvals WHERE status = 'pending'")
            ).fetchall()

        for row in rows:
            approval_id: str = row.id
            if approval_id not in self._waiters:
                self._waiters[approval_id] = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request(
        self,
        scope: str,
        agent_id: str,
        action: str,
        description: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> str:
        """Insert a pending approval row, emit approval_request event.

        Returns the approval_id (UUID string).
        """
        approval_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        def _write(conn: Connection) -> None:
            conn.execute(
                text(
                    "INSERT INTO approvals"
                    " (id, scope, requester_agent_id, operation, rationale, risk,"
                    "  status, created_at)"
                    " VALUES"
                    " (:id, :scope, :requester_agent_id, :operation, :rationale,"
                    "  :risk, 'pending', :created_at)"
                ),
                {
                    "id": approval_id,
                    "scope": scope,
                    "requester_agent_id": agent_id,
                    "operation": action,
                    "rationale": description,
                    "risk": (
                        metadata.get("risk", "unspecified")
                        if metadata
                        else "unspecified"
                    ),
                    "created_at": now,
                },
            )
            conn.commit()

        # NOTE: INSERT and event append are serialized through the single-writer queue.
        await self._db.write(_write)

        # Create waiter event before emitting the event so the waiter is
        # always available by the time decide() could be called.
        self._waiters[approval_id] = asyncio.Event()

        await self._event_service.append(
            scope,
            "approval_request",
            f"Approval requested: {action} by {agent_id}",
            agent_id=agent_id,
            payload={
                "approval_id": approval_id,
                "action": action,
                "description": description,
            },
        )

        return approval_id

    async def decide(
        self,
        approval_id: str,
        decision: Literal["approve", "deny"],
        comment: str = "",
        *,
        decided_by: str | None = None,
    ) -> ApprovalRecord:
        """Update the approval row status, emit approval_decision event, wake waiters.

        Returns the updated ApprovalRecord.
        Raises ValueError if approval_id does not exist.
        """
        now = datetime.now(UTC).isoformat()
        new_status = "approved" if decision == "approve" else "denied"

        existing = await self.get(approval_id)
        if existing is None:
            raise KeyError(f"approval {approval_id} not found")

        # Check-then-set race fix (P1-18): the UPDATE targets only pending rows.
        # If another concurrent decide() already wrote a decision, rowcount == 0
        # and we raise instead of silently overwriting the first decision.
        rows_updated: list[int] = []

        def _write(conn: Connection) -> None:
            result = conn.execute(
                text(
                    "UPDATE approvals"
                    " SET status = :status, comment = :comment,"
                    "     decided_at = :decided_at, decided_by = :decided_by"
                    " WHERE id = :id AND status = 'pending'"
                ),
                {
                    "status": new_status,
                    "comment": comment or None,
                    "decided_at": now,
                    "decided_by": decided_by,
                    "id": approval_id,
                },
            )
            conn.commit()
            rows_updated.append(result.rowcount)

        # NOTE: INSERT and event append are serialized through the single-writer queue.
        await self._db.write(_write)

        if rows_updated[0] == 0:
            raise ValueError(
                f"approval {approval_id} already decided"
            )

        record = await self.get(approval_id)
        if record is None:
            raise ValueError(f"Approval not found: {approval_id!r}")

        await self._event_service.append(
            record.scope,
            "approval_decision",
            f"Approval {decision}d: {record.operation} for {record.requester_agent_id}",
            payload={
                "approval_id": approval_id,
                "decision": decision,
                "comment": comment,
            },
        )

        # Wake any waiter blocking on this approval_id.
        waiter = self._waiters.get(approval_id)
        if waiter is not None:
            waiter.set()

        return record

    async def wait_for_decision(
        self,
        approval_id: str,
        *,
        timeout_s: float = 300.0,
    ) -> Literal["approve", "deny"]:
        """Block until a human posts a decision for approval_id.

        Returns "approve" or "deny".
        Raises ApprovalTimeoutError if no decision arrives within timeout_s.
        asyncio.CancelledError propagates through (not caught).
        """
        waiter = self._waiters.get(approval_id)
        if waiter is None:
            # Restart scenario: check DB immediately before blocking — the
            # decision may have arrived while this process was down.
            record = await self.get(approval_id)
            if record is not None and record.status in ("approved", "denied"):
                return "approve" if record.status == "approved" else "deny"
            waiter = asyncio.Event()
            self._waiters[approval_id] = waiter

        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout_s)
        except TimeoutError as exc:
            raise ApprovalTimeoutError(
                f"No decision for approval {approval_id!r} within {timeout_s}s"
            ) from exc

        # Decision arrived — read it from DB.
        record = await self.get(approval_id)
        if record is None:
            raise ApprovalTimeoutError(
                f"Approval {approval_id!r} vanished after decision"
            )

        if record.status == "approved":
            return "approve"
        return "deny"

    async def get(self, approval_id: str) -> ApprovalRecord | None:
        """Fetch one approval row by id. Returns None if not found."""
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT id, scope, requester_agent_id, operation, rationale,"
                    "       risk, status, decided_by, comment, created_at, decided_at"
                    " FROM approvals WHERE id = :id"
                ),
                {"id": approval_id},
            ).fetchone()

        if row is None:
            return None
        return _row_to_record(row)

    async def list_pending(self, scope: str) -> list[ApprovalRecord]:
        """Return all pending approvals for scope, ordered by creation time."""
        with self._db.read_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, scope, requester_agent_id, operation, rationale,"
                    "       risk, status, decided_by, comment, created_at, decided_at"
                    " FROM approvals"
                    " WHERE scope = :scope AND status = 'pending'"
                    " ORDER BY created_at ASC"
                ),
                {"scope": scope},
            ).fetchall()

        return [_row_to_record(row) for row in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_record(row: object) -> ApprovalRecord:
    """Convert a SQLAlchemy row to an ApprovalRecord."""
    return ApprovalRecord(
        id=row.id,  # type: ignore[attr-defined]
        scope=row.scope,  # type: ignore[attr-defined]
        requester_agent_id=row.requester_agent_id,  # type: ignore[attr-defined]
        operation=row.operation,  # type: ignore[attr-defined]
        rationale=row.rationale,  # type: ignore[attr-defined]
        risk=row.risk,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        decided_by=row.decided_by,  # type: ignore[attr-defined]
        comment=row.comment,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        decided_at=row.decided_at,  # type: ignore[attr-defined]
    )
