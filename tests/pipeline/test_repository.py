"""Tests for fleet.pipeline.repository.PipelineRepository (Task T3).

TDD: tests written FIRST; implementation follows.

Run:  uv run pytest tests/pipeline/test_repository.py -q
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy.exc

from fleet.db import DatabaseManager, init_db
from fleet.pipeline.models import PipelineRun, PipelineStage, RunStatus, StageStatus
from fleet.pipeline.repository import PipelineRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: pytest.TempPathFactory) -> AsyncIterator[DatabaseManager]:
    db_path = str(tmp_path / "test_repository.db")
    manager = await init_db(db_path)
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def repo(db: DatabaseManager) -> AsyncIterator[PipelineRepository]:
    yield PipelineRepository(db)


def _make_run(run_id: str = "run-1") -> PipelineRun:
    return PipelineRun(
        id=run_id,
        workflow_name="FULL_SDLC",
        idea="build a widget",
        scope="widget-scope",
        root_agent_id="agent-root",
        status=RunStatus.RUNNING,
        created_at="2026-06-30T00:00:00Z",
    )


def _make_stage(
    stage_id: str,
    run_id: str = "run-1",
    step_key: str = "design",
) -> PipelineStage:
    return PipelineStage(
        id=stage_id,
        run_id=run_id,
        step_key=step_key,
        role="designer",
        agent_id=None,
        task_id=None,
        idempotency_key=f"{run_id}:{step_key}",
        status=StageStatus.PENDING,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_step_key_for_same_run_raises_integrity_error(
    repo: PipelineRepository,
) -> None:
    """create_stage() with a duplicate (run_id, step_key) raises IntegrityError."""
    await repo.create_run(_make_run("run-dup"))
    await repo.create_stage(_make_stage("stage-1", run_id="run-dup", step_key="design"))

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await repo.create_stage(
            _make_stage("stage-2", run_id="run-dup", step_key="design")
        )


@pytest.mark.asyncio
async def test_create_run_then_get_run_round_trips_all_fields(
    repo: PipelineRepository,
) -> None:
    """create_run() followed by get_run() returns a matching PipelineRun."""
    run = _make_run("run-round-trip")
    await repo.create_run(run)

    fetched = await repo.get_run("run-round-trip")

    assert fetched == run


@pytest.mark.asyncio
async def test_get_stages_returns_all_stages_for_run_ordered_by_id(
    repo: PipelineRepository,
) -> None:
    """create_stage() twice, then get_stages() returns both, ordered by id."""
    await repo.create_run(_make_run("run-stages"))
    stage_a = _make_stage("stage-a", run_id="run-stages", step_key="design")
    stage_b = _make_stage("stage-b", run_id="run-stages", step_key="implement")
    await repo.create_stage(stage_a)
    await repo.create_stage(stage_b)

    fetched = await repo.get_stages("run-stages")

    assert fetched == [stage_a, stage_b]


@pytest.mark.asyncio
async def test_update_stage_status_changes_status_reflected_in_get_stages(
    repo: PipelineRepository,
) -> None:
    """update_stage_status() updates the stage row; get_stages() reflects it."""
    await repo.create_run(_make_run("run-update-stage"))
    stage = _make_stage("stage-update", run_id="run-update-stage", step_key="design")
    await repo.create_stage(stage)

    await repo.update_stage_status(
        "stage-update", StageStatus.RUNNING, agent_id="agent-x", task_id="task-y"
    )

    fetched = await repo.get_stages("run-update-stage")
    assert len(fetched) == 1
    assert fetched[0].status == StageStatus.RUNNING
    assert fetched[0].agent_id == "agent-x"
    assert fetched[0].task_id == "task-y"


@pytest.mark.asyncio
async def test_update_run_status_changes_status_reflected_in_get_run(
    repo: PipelineRepository,
) -> None:
    """update_run_status() updates the run row; get_run() reflects it."""
    await repo.create_run(_make_run("run-update-status"))

    await repo.update_run_status("run-update-status", RunStatus.DONE)

    fetched = await repo.get_run("run-update-status")
    assert fetched is not None
    assert fetched.status == RunStatus.DONE


@pytest.mark.asyncio
async def test_get_run_on_nonexistent_id_returns_none(
    repo: PipelineRepository,
) -> None:
    """get_run() for an id that was never created returns None."""
    fetched = await repo.get_run("does-not-exist")

    assert fetched is None
