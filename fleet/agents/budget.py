"""BudgetEnforcer — per-agent cost accumulation and limit enforcement (Task 2.4).

Public API:
    BudgetAction       — enum: OK | WARN | PAUSE
    BudgetEnforcer     — accumulates cost, checks soft/hard limits, emits events
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from fleet.agents.backends.protocol import TurnEnd
from fleet.db import DatabaseManager
from fleet.events.service import EventService


class BudgetAction(enum.Enum):
    OK = "ok"
    WARN = "warn"    # soft limit hit — emit budget_alert event, continue
    PAUSE = "pause"  # hard limit hit — agent must pause, wait for approval


class BudgetEnforcer:
    """Accumulates cost per turn_end; enforces soft and hard budget limits.

    Responsibilities:
      1. Atomically adds turn cost to agents.cost_usd in the DB.
      2. Reads the agent's budget limits and compares against the new total.
      3. Emits a budget_alert event when a limit is reached.
      4. Inserts an approval row when the hard limit is reached.
    """

    def __init__(
        self,
        agent_id: str,
        scope: str,
        db: DatabaseManager,
        event_service: EventService,
    ) -> None:
        self._agent_id = agent_id
        self._scope = scope
        self._db = db
        self._event_service = event_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def record_turn_cost(self, turn_end: TurnEnd) -> BudgetAction:
        """Accumulate turn cost and enforce budget limits.

        Steps:
          1. Add turn_end.cost_usd to agents.cost_usd (atomic DB update).
          2. Read the updated total and budget limits.
          3. If new total >= budget_hard_usd: emit budget_alert (level=hard),
             insert approval row, return BudgetAction.PAUSE.
          4. If new total >= budget_soft_usd (but < hard): emit budget_alert
             (level=soft), return BudgetAction.WARN.
          5. Else: return BudgetAction.OK.
          6. If both limits are NULL, always return BudgetAction.OK.
        """
        new_total, budget_soft, budget_hard = await self._accumulate_cost(
            turn_end.cost_usd
        )

        # No limits configured — nothing to enforce
        if budget_soft is None and budget_hard is None:
            return BudgetAction.OK

        # Hard limit check takes precedence
        if budget_hard is not None and new_total >= budget_hard:
            await self._emit_budget_alert("hard", new_total, budget_hard)
            await self._insert_approval()
            return BudgetAction.PAUSE

        # Soft limit check
        if budget_soft is not None and new_total >= budget_soft:
            await self._emit_budget_alert("soft", new_total, budget_soft)
            return BudgetAction.WARN

        return BudgetAction.OK

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _accumulate_cost(
        self, cost_usd: float
    ) -> tuple[float, float | None, float | None]:
        """Atomically add cost_usd to agents.cost_usd and return
        (new_total, budget_soft_usd, budget_hard_usd).
        """

        def _write(conn: Connection) -> tuple[float, float | None, float | None]:
            conn.execute(
                text(
                    "UPDATE agents SET cost_usd = cost_usd + :delta"
                    " WHERE id = :id"
                ),
                {"delta": cost_usd, "id": self._agent_id},
            )
            conn.commit()
            row = conn.execute(
                text(
                    "SELECT cost_usd, budget_soft_usd, budget_hard_usd"
                    " FROM agents WHERE id = :id"
                ),
                {"id": self._agent_id},
            ).fetchone()
            assert row is not None, f"Agent {self._agent_id!r} not found in DB"
            return (row.cost_usd, row.budget_soft_usd, row.budget_hard_usd)

        return await self._db.write(_write)

    async def _emit_budget_alert(
        self, level: str, cost_usd: float, limit_usd: float
    ) -> None:
        """Emit a budget_alert event for the given limit level."""
        await self._event_service.append(
            self._scope,
            "budget_alert",
            (
                f"Agent {self._agent_id} budget {level}:"
                f" ${cost_usd:.4f} / ${limit_usd:.4f}"
            ),
            agent_id=self._agent_id,
            payload={"level": level, "cost_usd": cost_usd, "limit_usd": limit_usd},
        )

    async def _insert_approval(self) -> None:
        """Insert a pending approval row requesting continuation past the hard limit.

        If a pending approval for this agent already exists, skip the insert to
        avoid duplicate rows on repeated hard-limit hits.
        """
        approval_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        def _write(conn: Connection) -> None:
            existing = conn.execute(
                text(
                    "SELECT id FROM approvals"
                    " WHERE requester_agent_id = :aid"
                    " AND status = 'pending'"
                    " AND operation = 'over_budget_continue'"
                    " LIMIT 1"
                ),
                {"aid": self._agent_id},
            ).fetchone()
            if existing:
                return  # already pending, don't create duplicate
            conn.execute(
                text(
                    "INSERT INTO approvals"
                    " (id, scope, requester_agent_id, operation, rationale,"
                    "  risk, status, created_at)"
                    " VALUES"
                    " (:id, :scope, :requester_agent_id, :operation, :rationale,"
                    "  :risk, 'pending', :created_at)"
                ),
                {
                    "id": approval_id,
                    "scope": self._scope,
                    "requester_agent_id": self._agent_id,
                    "operation": "over_budget_continue",
                    "rationale": "Hard budget limit reached",
                    "risk": "Continuing will exceed hard budget limit",
                    "created_at": now,
                },
            )
            conn.commit()

        await self._db.write(_write)
