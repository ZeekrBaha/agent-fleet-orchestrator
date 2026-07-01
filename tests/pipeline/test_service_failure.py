"""Tests for PipelineService's failure -> approval-queue routing (Task T8).

A stage's merge gate failing for a REAL reason (not just "no evidence yet")
must halt the run: route to ApprovalService, mark the stage failed, and set
the run's status to 'blocked' -- not 'failed', and not silently 'running'.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from fleet.agents.inbox import InboxService
from fleet.agents.service import AgentService
from fleet.approvals.service import ApprovalService
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService, create_event_service
from fleet.events.sse import SSEHub
from fleet.pipeline.models import RunStatus, StageStatus
from fleet.pipeline.repository import PipelineRepository
from fleet.pipeline.workflows import FULL_SDLC
from fleet.review.evidence import EvidenceService

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/pipeline/test_service_merge_gate.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_pipeline_failure.db"))
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
async def approval_service(
    db: DatabaseManager, event_service: EventService
) -> ApprovalService:
    return ApprovalService(db, event_service)


@pytest_asyncio.fixture
async def pipeline_service(
    db: DatabaseManager,
    repository: PipelineRepository,
    agent_service: AgentService,
    evidence_service: EvidenceService,
    approval_service: ApprovalService,
) -> Any:
    from fleet.pipeline.service import PipelineService

    return PipelineService(
        db=db,
        repo=repository,
        agent_service=agent_service,
        evidence_service=evidence_service,
        approval_service=approval_service,
    )


async def _advance_to_impl_running(
    pipeline_service: Any, repository: PipelineRepository
) -> Any:
    run = await pipeline_service.create_run(
        FULL_SDLC, idea="Build Failure Test", scope="pipeline-failure-test"
    )
    await pipeline_service.advance_run(run.id)
    stages = {s.step_key: s for s in await repository.get_stages(run.id)}
    assert stages["impl"].status == StageStatus.RUNNING
    return run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failing_check_creates_approval_and_blocks_run(
    pipeline_service: Any,
    repository: PipelineRepository,
    evidence_service: EvidenceService,
    approval_service: ApprovalService,
) -> None:
    run = await _advance_to_impl_running(pipeline_service, repository)
    stages = {s.step_key: s for s in await repository.get_stages(run.id)}
    impl_task_id = stages["impl"].task_id
    assert impl_task_id is not None

    await evidence_service.record_evidence(
        task_id=impl_task_id, check_name="pytest -q", status="fail", output="2 failed"
    )

    await pipeline_service.advance_run(run.id)

    updated_run = await repository.get_run(run.id)
    assert updated_run is not None
    assert updated_run.status == RunStatus.BLOCKED

    stages_after = {s.step_key: s for s in await repository.get_stages(run.id)}
    assert stages_after["impl"].status == StageStatus.FAILED

    pending = await approval_service.list_pending(scope=run.scope)
    assert len(pending) == 1
    assert run.id in pending[0].rationale or "impl" in pending[0].rationale


@pytest.mark.asyncio
async def test_missing_evidence_is_not_a_failure_and_does_not_block(
    pipeline_service: Any,
    repository: PipelineRepository,
    approval_service: ApprovalService,
) -> None:
    """No evidence recorded yet is 'not ready', not a failure -- must not
    create an approval or block the run."""
    run = await _advance_to_impl_running(pipeline_service, repository)

    await pipeline_service.advance_run(run.id)

    updated_run = await repository.get_run(run.id)
    assert updated_run is not None
    assert updated_run.status == RunStatus.RUNNING

    stages_after = {s.step_key: s for s in await repository.get_stages(run.id)}
    assert stages_after["impl"].status == StageStatus.RUNNING

    pending = await approval_service.list_pending(scope=run.scope)
    assert pending == []


@pytest.mark.asyncio
async def test_blocked_run_does_not_advance_other_stages(
    pipeline_service: Any,
    repository: PipelineRepository,
    evidence_service: EvidenceService,
) -> None:
    """Once a run is blocked, advance_run must not spawn any further stages."""
    run = await _advance_to_impl_running(pipeline_service, repository)
    stages = {s.step_key: s for s in await repository.get_stages(run.id)}
    impl_task_id = stages["impl"].task_id
    assert impl_task_id is not None

    await evidence_service.record_evidence(
        task_id=impl_task_id, check_name="pytest -q", status="fail", output="boom"
    )
    await pipeline_service.advance_run(run.id)

    # Calling advance_run again on a blocked run must be a no-op: nothing
    # spawns, review/fix/etc. all stay pending.
    await pipeline_service.advance_run(run.id)

    stages_after = {s.step_key: s for s in await repository.get_stages(run.id)}
    for key in ("review", "fix", "jqa", "sqa", "handoff"):
        assert stages_after[key].status == StageStatus.PENDING
