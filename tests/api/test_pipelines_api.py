"""Tests for the pipeline API routes (Task T10)."""
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_pipelines_api.db"))
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pipeline_returns_run_with_stages(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/pipelines",
        json={"workflow": "full-sdlc", "idea": "Build X", "scope": "api-test"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["workflow_name"] == "full-sdlc"
    assert data["status"] == "running"
    assert len(data["stages"]) == 9
    pm_stage = next(s for s in data["stages"] if s["step_key"] == "pm")
    assert pm_stage["status"] == "passed"  # create route also calls advance once


@pytest.mark.asyncio
async def test_create_pipeline_unknown_workflow_returns_400(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/api/pipelines",
        json={"workflow": "no-such-workflow", "idea": "Build X", "scope": "api-test"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_advance_unknown_run_returns_404(client: AsyncClient) -> None:
    resp = await client.post("/api/pipelines/nonexistent-run-id/advance")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_advance_route_unblocks_further_stages(client: AsyncClient) -> None:
    create_resp = await client.post(
        "/api/pipelines",
        json={"workflow": "full-sdlc", "idea": "Build Y", "scope": "api-test-2"},
    )
    run_id = create_resp.json()["id"]

    advance_resp = await client.post(f"/api/pipelines/{run_id}/advance")
    assert advance_resp.status_code == 200, advance_resp.text
    data = advance_resp.json()
    assert data["id"] == run_id
    assert len(data["stages"]) == 9


@pytest.mark.asyncio
async def test_get_pipeline_returns_run(client: AsyncClient) -> None:
    create_resp = await client.post(
        "/api/pipelines",
        json={"workflow": "full-sdlc", "idea": "Build Z", "scope": "api-test-3"},
    )
    run_id = create_resp.json()["id"]

    get_resp = await client.get(f"/api/pipelines/{run_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == run_id


@pytest.mark.asyncio
async def test_get_pipeline_unknown_run_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/api/pipelines/nonexistent-run-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_preview_returns_9_steps_and_never_spawns_agents(
    client: AsyncClient, agent_service: AgentService
) -> None:
    """FR6: preview makes zero spawn calls -- structurally guaranteed by the
    route having no PipelineService/AgentService dependency at all."""
    original_create_agent = AgentService.create_agent

    async def _fail_if_called(self: AgentService, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("preview must never call AgentService.create_agent")

    AgentService.create_agent = _fail_if_called  # type: ignore[method-assign]
    try:
        resp = await client.post(
            "/api/pipelines/preview",
            json={"workflow": "full-sdlc", "idea": "Build a Prompt Regression Lab"},
        )
    finally:
        AgentService.create_agent = original_create_agent  # type: ignore[method-assign]

    assert resp.status_code == 200, resp.text
    steps = resp.json()
    assert len(steps) == 9
    assert [s["step_key"] for s in steps] == [
        "pm", "ux", "arch", "impl", "review", "fix", "jqa", "sqa", "handoff",
    ]
    pm_step = steps[0]
    assert pm_step["title"] == "PM spec for Build a Prompt Regression Lab"
    assert pm_step["assignee"] == "pm-agent"
    assert pm_step["workspace"] == "scratch"
    assert pm_step["idempotency_key"].startswith("pipeline:")


@pytest.mark.asyncio
async def test_preview_empty_idea_returns_400(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/pipelines/preview", json={"workflow": "full-sdlc", "idea": "   "}
    )
    assert resp.status_code == 400
