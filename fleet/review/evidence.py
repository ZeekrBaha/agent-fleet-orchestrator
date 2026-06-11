"""EvidenceService — task lifecycle and validation evidence management.

Public API:
    EvidenceService(db) -> EvidenceService
    EvidenceService.create_task(scope, title, description, ...) -> str
    EvidenceService.record_evidence(task_id, check_name, status, output, ...) -> int
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
        check_name: str,
        status: str,
        output: str = "",
        *,
        recorded_by: str | None = None,
    ) -> int:
        """Insert a validation_evidence row. Returns the auto-increment id.

        Args:
            task_id:     Task this evidence belongs to.
            check_name:  Name of the check (e.g. "pytest", "ruff check .").
            status:      One of 'pass', 'fail', 'skip'.
            output:      Command output or summary text.
            recorded_by: agent_id of the recorder, or None if recorded by a human.
        """
        ts = datetime.now(UTC).isoformat()

        def _write(conn: Connection) -> int:
            result = conn.execute(
                text(
                    "INSERT INTO validation_evidence"
                    " (task_id, check_name, status, output, recorded_by, ts)"
                    " VALUES (:task_id, :check_name, :status, :output,"
                    "         :recorded_by, :ts)"
                ),
                {
                    "task_id": task_id,
                    "check_name": check_name,
                    "status": status,
                    "output": output,
                    "recorded_by": recorded_by,
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
                    "SELECT id, task_id, check_name, status, output, recorded_by, ts"
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
                "check_name": row.check_name,
                "status": row.status,
                "output": row.output,
                "recorded_by": row.recorded_by,
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
          - all evidence rows have status='pass' or status='skip' (none 'fail')

        Reviewer verdict logic (simple, no policy configuration required):
          - If a 'review' check row exists and its status='fail' → gate fails
            with reason "reviewer verdict: fail" (takes precedence over other
            failing checks so the message is clear).
          - If a 'review' check row exists and its status='pass' → counts as
            passing; other checks are still evaluated normally.
          - If no 'review' check row exists → reviewer is optional; gate
            proceeds based on other evidence.
        """
        task = await self.get_task(task_id)
        if task is None:
            return False, f"Task {task_id!r} not found"

        evidence = await self.list_evidence(task_id)
        if not evidence:
            return False, "No validation evidence recorded; run checks before merging"

        # Check reviewer verdict first — it produces a distinct failure message.
        review_rows = [e for e in evidence if e["check_name"] == "review"]
        if review_rows:
            # Use the most recent review row (highest id, list is ordered by id ASC).
            latest_review = review_rows[-1]
            if latest_review["status"] == "fail":
                return False, "reviewer verdict: fail"

        # Check all remaining rows for failures.
        failing = [e for e in evidence if e["status"] == "fail"]
        if failing:
            checks = ", ".join(str(e["check_name"]) for e in failing)
            return False, f"{len(failing)} failing check(s): {checks}"

        return True, "all checks passed"
