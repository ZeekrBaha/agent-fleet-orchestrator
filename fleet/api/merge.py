"""FastAPI router for merge gate and squash merge endpoints (Task 6.1).

Endpoints (all require Bearer token auth):
    POST /api/merge/{worktree_id}        — execute merge
    GET  /api/merge/{worktree_id}/check  — dry-run gate check (no mutations)

Dependency injection:
    set_merge_service(svc)  — called at application startup
    get_merge_service()     — used by route handlers
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from fleet.api.auth import require_token
from fleet.review.lock import MergeInProgressError
from fleet.review.merge import ConflictError, MergeGateError, MergeService

router = APIRouter(prefix="/api/merge", tags=["merge"])

# ---------------------------------------------------------------------------
# Dependency injection state (set once at startup)
# ---------------------------------------------------------------------------

_merge_service: MergeService | None = None


def get_merge_service() -> MergeService:
    """Dependency: return the active MergeService or raise RuntimeError."""
    if _merge_service is None:
        raise RuntimeError("MergeService not initialized")
    return _merge_service


def set_merge_service(svc: MergeService) -> None:
    """Set the global MergeService instance (called during app startup)."""
    global _merge_service
    _merge_service = svc


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class MergeRequest(BaseModel):
    """Body for POST /api/merge/{worktree_id}."""

    agent_id: str
    scope: str
    task_name: str | None = None


class MergeResponse(BaseModel):
    """Response for POST /api/merge/{worktree_id}."""

    commit_sha: str
    branch: str
    task_id: str


class GateCheckResponse(BaseModel):
    """Response for GET /api/merge/{worktree_id}/check."""

    can_merge: bool
    reason: str
    has_conflict: bool
    conflict_summary: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{worktree_id}", response_model=MergeResponse)
async def execute_merge(
    worktree_id: str,
    body: MergeRequest,
    _auth: Annotated[None, Depends(require_token)],
) -> MergeResponse:
    """Execute a squash merge for a worktree.

    Runs all gate checks (cleanliness, evidence, conflicts), then squash-merges
    the worktree branch into the default branch.

    Returns:
        MergeResponse with commit_sha, branch, task_id on success.

    Raises:
        409: Merge already in progress for this scope.
        422: Gate check or conflict failed.
    """
    svc = get_merge_service()
    try:
        result = await svc.execute_merge(
            worktree_id=worktree_id,
            agent_id=body.agent_id,
            scope=body.scope,
            task_name=body.task_name,
        )
    except MergeInProgressError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"merge in progress for scope {exc.scope}",
        ) from exc
    except MergeGateError as exc:
        raise HTTPException(
            status_code=422, detail=f"merge gate: {exc.reason}"
        ) from exc
    except ConflictError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"merge gate: {exc.conflict_result.summary[:500]}",
        ) from exc
    except ValueError as exc:
        # Maps ValueError (worktree not found → 404)
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return MergeResponse(
        commit_sha=result.commit_sha,
        branch=result.branch,
        task_id=result.task_id,
    )


@router.get("/{worktree_id}/check", response_model=GateCheckResponse)
async def check_merge_gate(
    worktree_id: str,
    _auth: Annotated[None, Depends(require_token)],
) -> GateCheckResponse:
    """Dry-run gate check — no mutations, no events.

    Returns:
        GateCheckResponse with can_merge, reason, has_conflict, conflict_summary.
    """
    svc = get_merge_service()
    try:
        status = await svc.check_gate(worktree_id)
    except ValueError as exc:
        # Maps ValueError (worktree not found → 404)
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return GateCheckResponse(
        can_merge=status.can_merge,
        reason=status.reason,
        has_conflict=status.has_conflict,
        conflict_summary=status.conflict_summary,
    )
