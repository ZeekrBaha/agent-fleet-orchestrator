"""PipelineRepository — persistence for PipelineRun and PipelineStage.

Public API:
    PipelineRepository.create_run(run) -> None
    PipelineRepository.create_stage(stage) -> None
    PipelineRepository.get_run(run_id) -> PipelineRun | None
    PipelineRepository.get_stages(run_id) -> list[PipelineStage]
    PipelineRepository.update_stage_status(stage_id, status, *,
                                            agent_id, task_id) -> None
    PipelineRepository.update_run_status(run_id, status) -> None
"""

from __future__ import annotations

from sqlalchemy import Connection, text

from fleet.db import DatabaseManager
from fleet.pipeline.models import PipelineRun, PipelineStage, RunStatus, StageStatus


class PipelineRepository:
    """Wraps a DatabaseManager for pipeline_run / pipeline_stage persistence."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_run(self, run: PipelineRun) -> None:
        """Insert one row into pipeline_run from a PipelineRun instance."""

        def _write(conn: Connection) -> None:
            sql = text(
                "INSERT INTO pipeline_run"
                " (id, workflow_name, idea, scope, root_agent_id, status, created_at)"
                " VALUES"
                " (:id, :workflow_name, :idea, :scope, :root_agent_id,"
                "  :status, :created_at)"
            )
            conn.execute(
                sql,
                {
                    "id": run.id,
                    "workflow_name": run.workflow_name,
                    "idea": run.idea,
                    "scope": run.scope,
                    "root_agent_id": run.root_agent_id,
                    "status": run.status.value,
                    "created_at": run.created_at,
                },
            )
            conn.commit()

        await self._db.write(_write)

    async def create_stage(self, stage: PipelineStage) -> None:
        """Insert one row into pipeline_stage from a PipelineStage instance.

        Raises ``sqlalchemy.exc.IntegrityError`` on a duplicate
        ``(run_id, step_key)`` pair — the caller is responsible for handling
        (or letting it propagate as) an idempotency violation.
        """

        def _write(conn: Connection) -> None:
            sql = text(
                "INSERT INTO pipeline_stage"
                " (id, run_id, step_key, role, agent_id, task_id,"
                "  idempotency_key, status)"
                " VALUES"
                " (:id, :run_id, :step_key, :role, :agent_id, :task_id,"
                "  :idempotency_key, :status)"
            )
            conn.execute(
                sql,
                {
                    "id": stage.id,
                    "run_id": stage.run_id,
                    "step_key": stage.step_key,
                    "role": stage.role,
                    "agent_id": stage.agent_id,
                    "task_id": stage.task_id,
                    "idempotency_key": stage.idempotency_key,
                    "status": stage.status.value,
                },
            )
            conn.commit()

        await self._db.write(_write)

    async def get_run(self, run_id: str) -> PipelineRun | None:
        """Read one pipeline_run row; return None if not found."""
        sql = text(
            "SELECT id, workflow_name, idea, scope, root_agent_id, status, created_at"
            " FROM pipeline_run WHERE id = :id"
        )
        with self._db.read_connection() as conn:
            row = conn.execute(sql, {"id": run_id}).fetchone()

        if row is None:
            return None

        return PipelineRun(
            id=row.id,
            workflow_name=row.workflow_name,
            idea=row.idea,
            scope=row.scope,
            root_agent_id=row.root_agent_id,
            status=RunStatus(row.status),
            created_at=row.created_at,
        )

    async def get_stages(self, run_id: str) -> list[PipelineStage]:
        """Read all stages for a run, ordered by id."""
        sql = text(
            "SELECT id, run_id, step_key, role, agent_id, task_id,"
            " idempotency_key, status"
            " FROM pipeline_stage WHERE run_id = :run_id ORDER BY id"
        )
        stages: list[PipelineStage] = []
        with self._db.read_connection() as conn:
            rows = conn.execute(sql, {"run_id": run_id}).fetchall()
            for row in rows:
                stages.append(
                    PipelineStage(
                        id=row.id,
                        run_id=row.run_id,
                        step_key=row.step_key,
                        role=row.role,
                        agent_id=row.agent_id,
                        task_id=row.task_id,
                        idempotency_key=row.idempotency_key,
                        status=StageStatus(row.status),
                    )
                )
        return stages

    async def update_stage_status(
        self,
        stage_id: str,
        status: StageStatus,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Update a stage's status; optionally set agent_id and/or task_id.

        Only columns whose keyword argument was passed a non-None value are
        updated in addition to ``status`` (which is always updated).
        """
        set_clauses = ["status = :status"]
        params: dict[str, object] = {"stage_id": stage_id, "status": status.value}

        if agent_id is not None:
            set_clauses.append("agent_id = :agent_id")
            params["agent_id"] = agent_id

        if task_id is not None:
            set_clauses.append("task_id = :task_id")
            params["task_id"] = task_id

        set_clause = ", ".join(set_clauses)

        def _write(conn: Connection) -> None:
            sql = text(f"UPDATE pipeline_stage SET {set_clause} WHERE id = :stage_id")
            conn.execute(sql, params)
            conn.commit()

        await self._db.write(_write)

    async def update_run_status(self, run_id: str, status: RunStatus) -> None:
        """Update a run's status."""

        def _write(conn: Connection) -> None:
            sql = text("UPDATE pipeline_run SET status = :status WHERE id = :run_id")
            conn.execute(sql, {"status": status.value, "run_id": run_id})
            conn.commit()

        await self._db.write(_write)
