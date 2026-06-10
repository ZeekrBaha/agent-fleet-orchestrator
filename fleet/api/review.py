"""FastAPI router for evidence review endpoints (Task 5.2).

Endpoints (all require Bearer token auth):
    POST /api/review/tasks              — create task
    GET  /api/review/tasks/{task_id}    — get task (404 if missing)
    GET  /api/review/tasks/{task_id}/evidence — list evidence rows
    GET  /api/review/tasks/{task_id}/gate     — merge-gate check

Dependency injection:
    set_evidence_service(svc)  — called at application startup
    get_evidence_service()     — used by route handlers
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from fleet.api.auth import require_token
from fleet.review.evidence import EvidenceService

router = APIRouter(prefix="/api/review", tags=["review"])

# ---------------------------------------------------------------------------
# Dependency injection state (set once at startup)
# ---------------------------------------------------------------------------

_evidence_svc: EvidenceService | None = None


def set_evidence_service(svc: EvidenceService) -> None:
    """Wire an EvidenceService instance into this module (called at startup)."""
    global _evidence_svc
    _evidence_svc = svc


def get_evidence_service() -> EvidenceService:
    """Return the wired EvidenceService; raises RuntimeError if not configured."""
    if _evidence_svc is None:
        raise RuntimeError(
            "EvidenceService not initialised — call set_evidence_service() first"
        )
    return _evidence_svc


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class TaskCreate(BaseModel):
    """Request body for POST /api/review/tasks."""

    scope: str
    title: str
    description: str
    owner_agent_id: str | None = None
    branch: str | None = None
    acceptance_criteria: list[str] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/tasks")
async def create_task(
    body: TaskCreate,
    _auth: Annotated[None, Depends(require_token)],
) -> dict[str, object]:
    """Create a new task. Returns {task_id: str}."""
    svc = get_evidence_service()
    task_id = await svc.create_task(
        scope=body.scope,
        title=body.title,
        description=body.description,
        owner_agent_id=body.owner_agent_id,
        branch=body.branch,
        acceptance_criteria=body.acceptance_criteria,
    )
    return {"task_id": task_id}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    _auth: Annotated[None, Depends(require_token)],
) -> dict[str, object]:
    """Get a task by id. Returns 404 if not found."""
    svc = get_evidence_service()
    task = await svc.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id!r} not found")
    return task


@router.get("/tasks/{task_id}/evidence")
async def list_evidence(
    task_id: str,
    _auth: Annotated[None, Depends(require_token)],
) -> list[dict[str, object]]:
    """List all evidence rows for a task, ordered by id."""
    svc = get_evidence_service()
    return await svc.list_evidence(task_id)


@router.get("/tasks/{task_id}/gate")
async def check_gate(
    task_id: str,
    _auth: Annotated[None, Depends(require_token)],
) -> dict[str, object]:
    """Check the merge gate for a task. Returns {can_merge: bool, reason: str}."""
    svc = get_evidence_service()
    can_merge, reason = await svc.check_merge_gate(task_id)
    return {"can_merge": can_merge, "reason": reason}
