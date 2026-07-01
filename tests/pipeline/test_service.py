"""Tests for PipelineService (Task T6): DAG-walk orchestration happy path.

TDD: written before fleet/pipeline/service.py exists.
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_pipeline_service.db"))
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_run_creates_run_and_all_pending_stages(
    pipeline_service: Any, repository: PipelineRepository
) -> None:
    run = await pipeline_service.create_run(
        FULL_SDLC, idea="Build X", scope="pipeline-test"
    )
    assert run.status == RunStatus.RUNNING
    assert run.root_agent_id

    stages = await repository.get_stages(run.id)
    assert len(stages) == 9
    assert {s.step_key for s in stages} == {
        "pm", "ux", "arch", "impl", "review", "fix", "jqa", "sqa", "handoff",
    }
    assert all(s.status == StageStatus.PENDING for s in stages)


@pytest.mark.asyncio
async def test_advance_run_completes_reachable_frontier_in_one_pass(
    pipeline_service: Any, repository: PipelineRepository
) -> None:
    """One advance_run() call walks forward until it hits a stage it can't
    complete synchronously (the impl worktree stage, left 'running' for T7).
    """
    run = await pipeline_service.create_run(
        FULL_SDLC, idea="Build a Prompt Regression Lab", scope="pipeline-test"
    )

    updated = await pipeline_service.advance_run(run.id)

    stages = {s.step_key: s for s in await repository.get_stages(run.id)}

    # pm -> ux, arch (fan-out) all complete synchronously (scratch stages).
    assert stages["pm"].status == StageStatus.PASSED
    assert stages["ux"].status == StageStatus.PASSED
    assert stages["arch"].status == StageStatus.PASSED

    # impl depends on BOTH ux and arch (fan-in) -- must not have started
    # before they were both passed, and is left 'running' (worktree gate
    # is T7's job, not T6's).
    assert stages["impl"].status == StageStatus.RUNNING
    assert stages["impl"].agent_id is not None

    # Nothing past impl is reachable yet.
    for key in ("review", "fix", "jqa", "sqa", "handoff"):
        assert stages[key].status == StageStatus.PENDING

    assert updated.status == RunStatus.RUNNING


@pytest.mark.asyncio
async def test_advance_run_never_starts_impl_before_both_fan_in_deps_pass(
    pipeline_service: Any, repository: PipelineRepository
) -> None:
    """Regression guard for the fan-in case: impl must not spawn if only one
    of its two dependencies (ux, arch) has passed.
    """
    run = await pipeline_service.create_run(
        FULL_SDLC, idea="Build Y", scope="pipeline-test"
    )

    # Manually drive pm and ux to passed, and put arch into 'running' (not
    # 'pending') to simulate "still in flight" without giving advance_run's
    # own cascading a chance to auto-complete it within this same call --
    # advance_run only ever spawns stages that are currently PENDING.
    stages = {s.step_key: s for s in await repository.get_stages(run.id)}
    await repository.update_stage_status(stages["pm"].id, StageStatus.PASSED)
    await repository.update_stage_status(stages["ux"].id, StageStatus.PASSED)
    await repository.update_stage_status(
        stages["arch"].id, StageStatus.RUNNING, agent_id="fake-in-flight-agent"
    )

    await pipeline_service.advance_run(run.id)

    stages_after = {s.step_key: s for s in await repository.get_stages(run.id)}
    assert stages_after["impl"].status == StageStatus.PENDING
    assert stages_after["impl"].agent_id is None


@pytest.mark.asyncio
async def test_advance_run_is_idempotent_when_nothing_new_is_eligible(
    pipeline_service: Any, repository: PipelineRepository
) -> None:
    run = await pipeline_service.create_run(
        FULL_SDLC, idea="Build Z", scope="pipeline-test"
    )
    await pipeline_service.advance_run(run.id)
    stages_first = {s.step_key: s.status for s in await repository.get_stages(run.id)}

    # Calling advance_run again before impl's gate resolves must not error
    # and must not change any stage's status (nothing new is eligible).
    await pipeline_service.advance_run(run.id)
    stages_second = {s.step_key: s.status for s in await repository.get_stages(run.id)}

    assert stages_first == stages_second
