"""FastAPI router for workspace (repository registry) management (Task 3.1).

Endpoints:
    POST   /api/workspaces             — register a repository
    GET    /api/workspaces             — list all registered repositories
    GET    /api/workspaces/{repo_id}   — get one repository
    DELETE /api/workspaces/{repo_id}   — unregister (204, does not delete files)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from fleet.api.auth import require_token
from fleet.workspace.service import RepositoryRecord, WorkspaceService

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_workspace_service: WorkspaceService | None = None


def get_workspace_service() -> WorkspaceService:
    """Dependency: return the active WorkspaceService or raise RuntimeError."""
    if _workspace_service is None:
        raise RuntimeError("WorkspaceService not initialized")
    return _workspace_service


def set_workspace_service(svc: WorkspaceService) -> None:
    """Set the global WorkspaceService instance (called during app startup)."""
    global _workspace_service
    _workspace_service = svc


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class RepoRegister(BaseModel):
    """Request body for POST /api/workspaces."""

    path: str
    default_branch: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=RepositoryRecord, status_code=201)
async def register_repo(
    body: RepoRegister,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> RepositoryRecord:
    """Register a git repository by filesystem path."""
    try:
        return await service.register_repo(
            body.path,
            default_branch=body.default_branch,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("", response_model=list[RepositoryRecord])
async def list_repos(
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> list[RepositoryRecord]:
    """List all registered repositories."""
    return await service.list_repos()


@router.get("/{repo_id}", response_model=RepositoryRecord)
async def get_repo(
    repo_id: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> RepositoryRecord:
    """Get a single repository by ID."""
    record = await service.get_repo(repo_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Repository not found")
    return record


@router.delete("/{repo_id}", status_code=204)
async def unregister_repo(
    repo_id: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> None:
    """Unregister a repository. Does NOT delete any files on disk."""
    record = await service.get_repo(repo_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Repository not found")
    await service.unregister_repo(repo_id)
