"""Tests for PipelineService's evidence + merge-gate wiring (Task T7).

Covers impl/fix (worktree) stages: create_task on spawn, check_merge_gate on
a later advance_run call before the stage can be marked passed.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from fleet.agents.inbox import InboxService
from fleet.agents.service import AgentService
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService, create_event_service
from fleet.events.sse import SSEHub
from fleet.pipeline.models import StageStatus
from fleet.pipeline.repository import PipelineRepository
from fleet.pipeline.workflows import FULL_SDLC
from fleet.review.evidence import EvidenceService

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/pipeline/test_service.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_pipeline_gate.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
def hub() -> SSEHub:
    return SSEHub()


@pytest_asyncio.fixture
async def event_service(db: DatabaseManager, hub: SSEHub) -> EventService:
    return create_event_service(db, hub)


@pytest_asyncio.fixture
async def agent_service(
    db: DatabaseManager, event_service: EventService
) -> AsyncIterator[AgentService]:
    inbox = InboxService(db)
    svc = AgentService(db, event_service, inbox)
    yield svc
    for session in list(svc._sessions.values()):
        task = session._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest_asyncio.fixture
async def repository(db: DatabaseManager) -> PipelineRepository:
    return PipelineRepository(db)


@pytest_asyncio.fixture
async def evidence_service(db: DatabaseManager) -> EvidenceService:
    return EvidenceService(db, gate_require_reviewer=False)


@pytest_asyncio.fixture
async def pipeline_service(
    db: DatabaseManager,
    repository: PipelineRepository,
    agent_service: AgentService,
    evidence_service: EvidenceService,
) -> Any:
    from fleet.pipeline.service import PipelineService

    return PipelineService(
        db=db,
        repo=repository,
        agent_service=agent_service,
        evidence_service=evidence_service,
    )


async def _advance_to_impl_running(
    pipeline_service: Any, repository: PipelineRepository
) -> Any:
    """Drive a fresh run forward until 'impl' is spawned (running, has a task_id)."""
    run = await pipeline_service.create_run(
        FULL_SDLC, idea="Build Gate Test", scope="pipeline-gate-test"
    )
    await pipeline_service.advance_run(run.id)
    stages = {s.step_key: s for s in await repository.get_stages(run.id)}
    assert stages["impl"].status == StageStatus.RUNNING
    assert stages["impl"].task_id is not None
    return run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_impl_spawn_creates_evidence_task(
    pipeline_service: Any, repository: PipelineRepository
) -> None:
    run = await _advance_to_impl_running(pipeline_service, repository)
    stages = {s.step_key: s for s in await repository.get_stages(run.id)}
    assert stages["impl"].task_id
    assert stages["impl"].status == StageStatus.RUNNING


@pytest.mark.asyncio
async def test_failing_evidence_blocks_advance_past_impl(
    pipeline_service: Any,
    repository: PipelineRepository,
    evidence_service: EvidenceService,
) -> None:
    run = await _advance_to_impl_running(pipeline_service, repository)
    stages = {s.step_key: s for s in await repository.get_stages(run.id)}
    impl_task_id = stages["impl"].task_id
    assert impl_task_id is not None

    await evidence_service.record_evidence(
        task_id=impl_task_id, check_name="pytest -q", status="fail", output="2 failed"
    )

    await pipeline_service.advance_run(run.id)

    stages_after = {s.step_key: s for s in await repository.get_stages(run.id)}
    assert stages_after["impl"].status == StageStatus.RUNNING
    assert stages_after["review"].status == StageStatus.PENDING


@pytest.mark.asyncio
async def test_passing_evidence_advances_impl_to_passed_and_unblocks_review(
    pipeline_service: Any,
    repository: PipelineRepository,
    evidence_service: EvidenceService,
) -> None:
    run = await _advance_to_impl_running(pipeline_service, repository)
    stages = {s.step_key: s for s in await repository.get_stages(run.id)}
    impl_task_id = stages["impl"].task_id
    assert impl_task_id is not None

    await evidence_service.record_evidence(
        task_id=impl_task_id, check_name="pytest -q", status="pass", output="all green"
    )

    await pipeline_service.advance_run(run.id)

    stages_after = {s.step_key: s for s in await repository.get_stages(run.id)}
    assert stages_after["impl"].status == StageStatus.PASSED
    # review has no other deps -- it should now be eligible and spawned in
    # this same call (review is a scratch stage, completes synchronously).
    assert stages_after["review"].status == StageStatus.PASSED
