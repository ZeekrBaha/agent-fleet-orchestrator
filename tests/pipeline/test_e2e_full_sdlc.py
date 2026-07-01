"""Task T11 — full end-to-end pipeline integration test.

Runs FULL_SDLC start to finish through the real API routes (not
service-layer calls directly), against MockBackend. Evidence for the two
worktree stages (impl, fix) is recorded directly via EvidenceService --
there is no dedicated "record evidence for a pipeline stage" API route
(evidence recording is a tool call a real agent makes; MockBackend's
scripted turns for pipeline stages don't call tools -- see T6). This is a
known scope limit, not a gap in this test.

This is the target of the constitution's mandatory independent refutation
pass (validation-plan.md) -- a fresh reviewer should try hardest to break
the claim that this test proves the port actually works end-to-end on
fleet's own engine, with no Hermes Kanban dependency anywhere in the path.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fleet.agents.inbox import InboxService
from fleet.agents.service import AgentService
from fleet.api.auth import require_token
from fleet.api.pipelines import router, set_pipeline_repository, set_pipeline_service
from fleet.approvals.service import ApprovalService
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService, create_event_service
from fleet.events.sse import SSEHub
from fleet.pipeline.repository import PipelineRepository
from fleet.pipeline.service import PipelineService
from fleet.review.evidence import EvidenceService

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/api/test_pipelines_api.py)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_e2e_full_sdlc.db"))
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
) -> PipelineService:
    return PipelineService(
        db=db,
        repo=repository,
        agent_service=agent_service,
        evidence_service=evidence_service,
        approval_service=approval_service,
        event_service=event_service,
    )


@pytest_asyncio.fixture
async def client(
    pipeline_service: PipelineService, repository: PipelineRepository
) -> AsyncIterator[AsyncClient]:
    set_pipeline_service(pipeline_service)
    set_pipeline_repository(repository)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_token] = lambda: None

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_sdlc_runs_start_to_finish_through_the_real_api(
    client: AsyncClient,
    evidence_service: EvidenceService,
    repository: PipelineRepository,
) -> None:
    # 1. Create the run via the real API -- this also advances it once.
    create_resp = await client.post(
        "/api/pipelines",
        json={
            "workflow": "full-sdlc",
            "idea": "Build a Prompt Regression Lab",
            "scope": "e2e-full-sdlc",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    run_id = create_resp.json()["id"]

    def _stages_by_key(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {s["step_key"]: s for s in payload["stages"]}

    stages = _stages_by_key(create_resp.json())
    assert stages["pm"]["status"] == "passed"
    assert stages["ux"]["status"] == "passed"
    assert stages["arch"]["status"] == "passed"
    assert stages["impl"]["status"] == "running"
    impl_task_id = stages["impl"]["task_id"]
    assert impl_task_id, "impl must have a real evidence task_id, not mocked away"

    # 2. Record real evidence for impl (a real DB row via EvidenceService,
    #    not a mock) and confirm it actually landed.
    await evidence_service.record_evidence(
        task_id=impl_task_id, check_name="pytest -q", status="pass", output="green"
    )
    rows = await evidence_service.list_evidence(impl_task_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "pass"

    # 3. Advance via the real API -- impl's gate opens, review completes
    #    synchronously (scratch stage), fix spawns (worktree stage again).
    advance_resp_1 = await client.post(f"/api/pipelines/{run_id}/advance")
    assert advance_resp_1.status_code == 200, advance_resp_1.text
    stages = _stages_by_key(advance_resp_1.json())
    assert stages["impl"]["status"] == "passed"
    assert stages["review"]["status"] == "passed"
    assert stages["fix"]["status"] == "running"
    fix_task_id = stages["fix"]["task_id"]
    assert fix_task_id, "fix must have a real evidence task_id, not mocked away"
    assert fix_task_id != impl_task_id

    # 4. Record real evidence for fix.
    await evidence_service.record_evidence(
        task_id=fix_task_id, check_name="pytest -q", status="pass", output="green"
    )

    # 5. Advance again -- fix's gate opens, jqa/sqa/handoff cascade complete
    #    (all scratch stages, chained dependency), run reaches 'done'.
    advance_resp_2 = await client.post(f"/api/pipelines/{run_id}/advance")
    assert advance_resp_2.status_code == 200, advance_resp_2.text
    final = advance_resp_2.json()
    stages = _stages_by_key(final)

    assert final["status"] == "done"
    for step_key in (
        "pm", "ux", "arch", "impl", "review", "fix", "jqa", "sqa", "handoff",
    ):
        assert stages[step_key]["status"] == "passed", (
            f"{step_key} expected passed, got {stages[step_key]['status']}"
        )

    # 6. Confirm the final state via a fresh GET too (not just the advance
    #    response), and confirm the DB-level repository agrees.
    get_resp = await client.get(f"/api/pipelines/{run_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "done"

    db_run = await repository.get_run(run_id)
    assert db_run is not None
    assert db_run.status.value == "done"
