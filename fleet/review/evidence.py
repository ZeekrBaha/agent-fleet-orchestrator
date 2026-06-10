"""EvidenceService — task lifecycle and validation evidence management.

Public API:
    EvidenceService(db) -> EvidenceService
    EvidenceService.create_task(scope, title, description, ...) -> str
    EvidenceService.record_evidence(task_id, command, exit_code, summary, ...) -> int
    EvidenceService.get_task(task_id) -> dict | None
    EvidenceService.list_evidence(task_id) -> list[dict]
    EvidenceService.check_merge_gate(task_id) -> tuple[bool, str]
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from fleet.db import DatabaseManager


class EvidenceService:
    """Manages tasks and their validation evidence records."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_task(
        self,
        scope: str,
        title: str,
        description: str,
        *,
        owner_agent_id: str | None = None,
        branch: str | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> str:
        """Insert a task row with status=open. Returns task_id (uuid4 string)."""
        task_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        criteria_json = json.dumps(acceptance_criteria or [])

        def _write(conn: Connection) -> None:
            conn.execute(
                text(
                    "INSERT INTO tasks"
                    " (id, scope, title, description, status,"
                    "  owner_agent_id, branch, acceptance_criteria_json,"
                    "  created_at, updated_at)"
                    " VALUES"
                    " (:id, :scope, :title, :description, 'open',"
                    "  :owner_agent_id, :branch, :criteria_json,"
                    "  :now, :now)"
                ),
                {
                    "id": task_id,
                    "scope": scope,
                    "title": title,
                    "description": description,
                    "owner_agent_id": owner_agent_id,
                    "branch": branch,
                    "criteria_json": criteria_json,
                    "now": now,
                },
            )
            conn.commit()

        await self._db.write(_write)
        return task_id

    async def record_evidence(
        self,
        task_id: str,
        command: str,
        exit_code: int,
        summary: str,
        *,
        skipped: str | None = None,
        residual_risk: str | None = None,
    ) -> int:
        """Insert a validation_evidence row. Returns the auto-increment id."""
        ts = datetime.now(UTC).isoformat()

        def _write(conn: Connection) -> int:
            result = conn.execute(
                text(
                    "INSERT INTO validation_evidence"
                    " (task_id, command, exit_code, summary,"
                    "  skipped, residual_risk, ts)"
                    " VALUES (:task_id, :command, :exit_code, :summary,"
                    "         :skipped, :residual_risk, :ts)"
                ),
                {
                    "task_id": task_id,
                    "command": command,
                    "exit_code": exit_code,
                    "summary": summary,
                    "skipped": skipped,
                    "residual_risk": residual_risk,
                    "ts": ts,
                },
            )
            conn.commit()
            last_id = result.lastrowid
            if last_id is None:
                raise RuntimeError("INSERT did not return a rowid")
            return int(last_id)

        return await self._db.write(_write)

    async def get_task(self, task_id: str) -> dict[str, object] | None:
        """Return the task row as a dict, or None if not found."""
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT id, scope, title, description, status,"
                    "  owner_agent_id, branch, acceptance_criteria_json,"
                    "  created_at, updated_at"
                    " FROM tasks WHERE id = :id"
                ),
                {"id": task_id},
            ).fetchone()

        if row is None:
            return None

        return {
            "id": row.id,
            "scope": row.scope,
            "title": row.title,
            "description": row.description,
            "status": row.status,
            "owner_agent_id": row.owner_agent_id,
            "branch": row.branch,
            "acceptance_criteria": json.loads(row.acceptance_criteria_json),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def list_evidence(self, task_id: str) -> list[dict[str, object]]:
        """Return all evidence rows for a task, ordered by id ascending."""
        with self._db.read_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, task_id, command, exit_code, summary,"
                    "  skipped, residual_risk, ts"
                    " FROM validation_evidence"
                    " WHERE task_id = :task_id"
                    " ORDER BY id ASC"
                ),
                {"task_id": task_id},
            ).fetchall()

        return [
            {
                "id": row.id,
                "task_id": row.task_id,
                "command": row.command,
                "exit_code": row.exit_code,
                "summary": row.summary,
                "skipped": row.skipped,
                "residual_risk": row.residual_risk,
                "ts": row.ts,
            }
            for row in rows
        ]

    async def check_merge_gate(self, task_id: str) -> tuple[bool, str]:
        """Check whether a task is ready to merge.

        Returns (can_merge, reason).
        can_merge=True only when:
          - task exists
          - at least one evidence row exists
          - all evidence rows have exit_code=0
        """
        task = await self.get_task(task_id)
        if task is None:
            return False, f"Task {task_id!r} not found"

        evidence = await self.list_evidence(task_id)
        if not evidence:
            return False, "No validation evidence recorded; run checks before merging"

        failing = [e for e in evidence if e["exit_code"] != 0]
        if failing:
            commands = ", ".join(str(e["command"]) for e in failing)
            return False, f"{len(failing)} failing check(s): {commands}"

        return True, f"All {len(evidence)} check(s) passed; merge gate open"
