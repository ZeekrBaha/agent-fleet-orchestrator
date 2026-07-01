"""FastAPI router for pipeline-run orchestration (Task T10).

Endpoints:
    POST /api/pipelines             — create a run, then advance it once
    POST /api/pipelines/{id}/advance — re-check and spawn newly-unblocked stages
    GET  /api/pipelines/{id}        — current run + all stages
    POST /api/pipelines/preview     — plan-only, zero spawn calls (FR6)
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from fleet.api.auth import require_token
from fleet.pipeline.models import PipelineRun, PipelineStage
from fleet.pipeline.planner import EmptyIdeaError, build_plan
from fleet.pipeline.repository import PipelineRepository
from fleet.pipeline.service import PipelineService
from fleet.pipeline.workflows import load as load_workflow

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_pipeline_service: PipelineService | None = None
_pipeline_repository: PipelineRepository | None = None


def get_pipeline_service() -> PipelineService:
    """Dependency: return the active PipelineService or raise RuntimeError."""
    if _pipeline_service is None:
        raise RuntimeError("PipelineService not initialized")
    return _pipeline_service


def set_pipeline_service(svc: PipelineService) -> None:
    """Set the global PipelineService instance (called during app startup)."""
    global _pipeline_service
    _pipeline_service = svc


def get_pipeline_repository() -> PipelineRepository:
    """Dependency: return the active PipelineRepository or raise RuntimeError."""
    if _pipeline_repository is None:
        raise RuntimeError("PipelineRepository not initialized")
    return _pipeline_repository


def set_pipeline_repository(repo: PipelineRepository) -> None:
    """Set the global PipelineRepository instance (called during app startup)."""
    global _pipeline_repository
    _pipeline_repository = repo


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PipelineCreate(BaseModel):
    """Request body for POST /api/pipelines."""

    workflow: str
    idea: str
    scope: str


class PipelinePreviewRequest(BaseModel):
    """Request body for POST /api/pipelines/preview."""

    workflow: str
    idea: str


class PipelineStageOut(BaseModel):
    """Response shape for one PipelineStage row."""

    id: str
    step_key: str
    role: str
    agent_id: str | None
    task_id: str | None
    status: str


class PipelineRunOut(BaseModel):
    """Response shape for a PipelineRun plus its stages."""

    id: str
    workflow_name: str
    idea: str
    scope: str
    root_agent_id: str
    status: str
    created_at: str
    stages: list[PipelineStageOut]


class PlannedStepOut(BaseModel):
    """Response shape for one previewed step (no run/stage created)."""

    step_key: str
    title: str
    assignee: str
    workspace: str
    idempotency_key: str


def _to_response(run: PipelineRun, stages: list[PipelineStage]) -> PipelineRunOut:
    return PipelineRunOut(
        id=run.id,
        workflow_name=run.workflow_name,
        idea=run.idea,
        scope=run.scope,
        root_agent_id=run.root_agent_id,
        status=run.status.value,
        created_at=run.created_at,
        stages=[
            PipelineStageOut(
                id=s.id,
                step_key=s.step_key,
                role=s.role,
                agent_id=s.agent_id,
                task_id=s.task_id,
                status=s.status.value,
            )
            for s in stages
        ],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=PipelineRunOut, status_code=201)
async def create_pipeline(
    body: PipelineCreate,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[PipelineService, Depends(get_pipeline_service)],
    repo: Annotated[PipelineRepository, Depends(get_pipeline_repository)],
) -> PipelineRunOut:
    """Create a pipeline run and immediately advance it once."""
    try:
        workflow = load_workflow(body.workflow)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run = await service.create_run(workflow, body.idea, body.scope)
    run = await service.advance_run(run.id)
    stages = await repo.get_stages(run.id)
    return _to_response(run, stages)


@router.post("/{run_id}/advance", response_model=PipelineRunOut)
async def advance_pipeline(
    run_id: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[PipelineService, Depends(get_pipeline_service)],
    repo: Annotated[PipelineRepository, Depends(get_pipeline_repository)],
) -> PipelineRunOut:
    """Re-check the DAG and spawn any newly-unblocked stages."""
    try:
        run = await service.advance_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    stages = await repo.get_stages(run.id)
    return _to_response(run, stages)


@router.get("/{run_id}", response_model=PipelineRunOut)
async def get_pipeline(
    run_id: str,
    _auth: Annotated[None, Depends(require_token)],
    repo: Annotated[PipelineRepository, Depends(get_pipeline_repository)],
) -> PipelineRunOut:
    """Get the current run state plus all stages."""
    run = await repo.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    stages = await repo.get_stages(run_id)
    return _to_response(run, stages)


@router.post("/preview", response_model=list[PlannedStepOut])
async def preview_pipeline(
    body: PipelinePreviewRequest,
    _auth: Annotated[None, Depends(require_token)],
) -> list[PlannedStepOut]:
    """Plan-only preview: zero spawn calls (FR6). No PipelineService or
    AgentService dependency is declared for this route -- it structurally
    cannot spawn an agent even if it wanted to."""
    try:
        workflow = load_workflow(body.workflow)
        plan = build_plan(body.idea, workflow)
    except (ValueError, EmptyIdeaError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return [PlannedStepOut(**asdict(step)) for step in plan]
