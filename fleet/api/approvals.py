"""FastAPI router for approval queue endpoints (Task 6.2).

Routes:
    GET  /api/approvals?scope=<scope>       — list pending approvals
    POST /api/approvals/{approval_id}/decide — submit a decision (requires auth)
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from fleet.api.auth import require_token
from fleet.approvals.service import ApprovalService
from fleet.models import ApprovalRecord

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

# ---------------------------------------------------------------------------
# Dependency injection state
# ---------------------------------------------------------------------------

_approval_svc: ApprovalService | None = None


def set_approval_service(svc: ApprovalService) -> None:
    """Wire the ApprovalService instance into this module (called at startup)."""
    global _approval_svc
    _approval_svc = svc


def get_approval_service() -> ApprovalService:
    """Return the injected ApprovalService; raises RuntimeError if not configured."""
    if _approval_svc is None:
        raise RuntimeError(
            "ApprovalService not initialised — call set_approval_service() first"
        )
    return _approval_svc


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------


class DecideRequest(BaseModel):
    """Body for POST /api/approvals/{id}/decide."""

    decision: Literal["approve", "deny"]
    comment: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ApprovalRecord])
async def list_pending_approvals(
    scope: str,
    _auth: Annotated[None, Depends(require_token)],
) -> list[ApprovalRecord]:
    """List all pending approvals for the given scope."""
    svc = get_approval_service()
    return await svc.list_pending(scope)


@router.post("/{approval_id}/decide", response_model=ApprovalRecord)
async def decide_approval(
    approval_id: str,
    body: DecideRequest,
    _auth: Annotated[None, Depends(require_token)],
) -> ApprovalRecord:
    """Submit a human decision (approve/deny) for an approval request."""
    svc = get_approval_service()

    try:
        record: ApprovalRecord = await svc.decide(
            approval_id,
            body.decision,
            comment=body.comment,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return record
