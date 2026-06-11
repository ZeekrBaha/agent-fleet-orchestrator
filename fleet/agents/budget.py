"""BudgetEnforcer — per-agent cost accumulation and limit enforcement (Task 2.4).

Public API:
    BudgetAction       — enum: OK | WARN | PAUSE
    BudgetEnforcer     — accumulates cost, checks soft/hard limits, emits events
"""

from __future__ import annotations

import enum

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

    Note: approval row creation on hard-limit is handled by AgentSession via
    approval_svc.request(), not by BudgetEnforcer.
    """

    def __init__(
        self,
        agent_id: str,
        scope: str,
        db: DatabaseManager,
        event_service: EventService,
        scope_budget_hard_usd: float | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._scope = scope
        self._db = db
        self._event_service = event_service
        self._scope_budget_hard_usd = scope_budget_hard_usd

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_pre_turn(self) -> BudgetAction:
        """Check budget BEFORE starting a turn — no cost accumulation.

        Returns PAUSE if the agent is already at its hard limit, or if the
        per-scope aggregate cap is exceeded.  Returns OK otherwise.
        """
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT cost_usd, budget_hard_usd FROM agents WHERE id = :id"
                ),
                {"id": self._agent_id},
            ).fetchone()

        if row is None:
            return BudgetAction.OK

        cost_usd: float = row.cost_usd or 0.0
        budget_hard: float | None = row.budget_hard_usd

        if budget_hard is not None and cost_usd >= budget_hard:
            return BudgetAction.PAUSE

        if self._scope_budget_hard_usd is not None:
            with self._db.read_connection() as conn:
                scope_row = conn.execute(
                    text(
                        "SELECT COALESCE(SUM(cost_usd), 0.0)"
                        " FROM agents WHERE scope = :scope"
                    ),
                    {"scope": self._scope},
                ).fetchone()
            scope_total: float = scope_row[0] if scope_row else 0.0
            if scope_total >= self._scope_budget_hard_usd:
                await self._emit_budget_alert(
                    "scope", scope_total, self._scope_budget_hard_usd
                )
                return BudgetAction.PAUSE

        return BudgetAction.OK

    async def record_turn_cost(self, turn_end: TurnEnd) -> BudgetAction:
        """Accumulate turn cost and enforce budget limits.

        Steps:
          1. Add turn_end.cost_usd to agents.cost_usd (atomic DB update).
          2. Read the updated total and budget limits.
          3. If new total >= budget_hard_usd: emit budget_alert (level=hard),
             return BudgetAction.PAUSE.  (AgentSession handles approval creation.)
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

