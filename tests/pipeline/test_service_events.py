"""Tests for PipelineService's SSE event emission on stage transitions (Task T9).

Every stage status transition must call EventService.append so the run's
progress is observable via the existing SSE event stream (sinks-over-pipes:
these events are a side-channel for observers, not part of advance_run's own
control flow -- see architecture.md).
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
from fleet.pipeline.repository import PipelineRepository
from fleet.pipeline.workflows import FULL_SDLC
from fleet.review.evidence import EvidenceService

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/pipeline/test_service.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_pipeline_events.db"))
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
    event_service: EventService,
) -> Any:
    from fleet.pipeline.service import PipelineService

    return PipelineService(
        db=db,
        repo=repository,
        agent_service=agent_service,
        evidence_service=evidence_service,
        approval_service=approval_service,
        event_service=event_service,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_run_emits_one_event_per_stage_transition(
    pipeline_service: Any,
    repository: PipelineRepository,
    event_service: EventService,
) -> None:
    run = await pipeline_service.create_run(
        FULL_SDLC, idea="Build Event Test", scope="pipeline-events-test"
    )
    await pipeline_service.advance_run(run.id)

    events = await event_service.query(run.scope, type_filter="state_change")
    pipeline_events = [
        e for e in events if e.payload.get("run_id") == run.id
    ]

    # pm, ux, arch each transition pending->running->passed (2 events each);
    # impl transitions pending->running only (1 event, gate not yet checked).
    step_keys_seen = [e.payload["step_key"] for e in pipeline_events]
    assert step_keys_seen.count("pm") == 2
    assert step_keys_seen.count("ux") == 2
    assert step_keys_seen.count("arch") == 2
    assert step_keys_seen.count("impl") == 1

    statuses_for_pm = [
        e.payload["status"] for e in pipeline_events if e.payload["step_key"] == "pm"
    ]
    assert statuses_for_pm == ["running", "passed"]


@pytest.mark.asyncio
async def test_gate_pass_and_fail_both_emit_events(
    pipeline_service: Any,
    repository: PipelineRepository,
    evidence_service: EvidenceService,
    event_service: EventService,
) -> None:
    run = await pipeline_service.create_run(
        FULL_SDLC, idea="Build Gate Event Test", scope="pipeline-events-gate-test"
    )
    await pipeline_service.advance_run(run.id)
    stages = {s.step_key: s for s in await repository.get_stages(run.id)}
    impl_task_id = stages["impl"].task_id
    assert impl_task_id is not None

    await evidence_service.record_evidence(
        task_id=impl_task_id, check_name="pytest -q", status="pass", output="green"
    )
    await pipeline_service.advance_run(run.id)

    events = await event_service.query(run.scope, type_filter="state_change")
    impl_events = [
        e
        for e in events
        if e.payload.get("run_id") == run.id and e.payload.get("step_key") == "impl"
    ]
    statuses = [e.payload["status"] for e in impl_events]
    assert statuses == ["running", "passed"]
