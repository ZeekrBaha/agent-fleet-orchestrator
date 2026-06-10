"""FastAPI router for workspace management (Tasks 3.1 + 3.2).

Endpoints:
    POST   /api/workspaces                                    — register a repo
    GET    /api/workspaces                                    — list all repos
    GET    /api/workspaces/{repo_id}                          — get one repo
    DELETE /api/workspaces/{repo_id}                          — unregister (204)
    POST   /api/workspaces/{repo_id}/worktrees                — create a worktree
    GET    /api/workspaces/{repo_id}/worktrees                — list worktrees
    DELETE /api/workspaces/{repo_id}/worktrees/{worktree_id}  — remove (204)
    GET    /api/workspaces/{repo_id}/worktrees/{worktree_id}/wip — WIP report
    POST   /api/workspaces/{repo_id}/dirty-action             — handle dirty repo
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from fleet.api.auth import require_token
from fleet.models import WorktreeRecord
from fleet.workspace.service import RepositoryRecord, WorkspaceService
from fleet.workspace.worktree_service import (
    DirtyRepoError,
    OverlapError,
    WorktreeError,
    WorktreeService,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_workspace_service: WorkspaceService | None = None
_worktree_service: WorktreeService | None = None


def get_workspace_service() -> WorkspaceService:
    """Dependency: return the active WorkspaceService or raise RuntimeError."""
    if _workspace_service is None:
        raise RuntimeError("WorkspaceService not initialized")
    return _workspace_service


def set_workspace_service(svc: WorkspaceService) -> None:
    """Set the global WorkspaceService instance (called during app startup)."""
    global _workspace_service
    _workspace_service = svc


def get_worktree_service() -> WorktreeService:
    """Dependency: return the active WorktreeService or raise RuntimeError."""
    if _worktree_service is None:
        raise RuntimeError("WorktreeService not initialized")
    return _worktree_service


def set_worktree_service(svc: WorktreeService) -> None:
    """Set the global WorktreeService instance (called during app startup)."""
    global _worktree_service
    _worktree_service = svc


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class RepoRegister(BaseModel):
    """Request body for POST /api/workspaces."""

    path: str
    default_branch: str | None = None


class WorktreeCreate(BaseModel):
    """Request body for POST /api/workspaces/{repo_id}/worktrees."""

    agent_id: str
    task_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    owned_paths: list[str] = Field(default_factory=list)


class DirtyAction(BaseModel):
    """Request body for POST /api/workspaces/{repo_id}/dirty-action."""

    option: str  # "continue_dirty" | "stash" | "commit" | "cancel"
    commit_message: str | None = None


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


# ---------------------------------------------------------------------------
# Worktree endpoints (Task 3.2)
# ---------------------------------------------------------------------------


@router.post(
    "/{repo_id}/worktrees",
    response_model=WorktreeRecord,
    status_code=201,
)
async def create_worktree(
    repo_id: str,
    body: WorktreeCreate,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[WorktreeService, Depends(get_worktree_service)],
) -> WorktreeRecord:
    """Create a new git worktree for an agent task."""
    try:
        return await service.create_worktree(
            repo_id=repo_id,
            agent_id=body.agent_id,
            task_id=body.task_id,
            name=body.name,
            owned_paths=body.owned_paths,
        )
    except DirtyRepoError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "options": exc.options},
        ) from exc
    except OverlapError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "conflicting_worktree_id": exc.conflicting_worktree_id,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorktreeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{repo_id}/worktrees", response_model=list[WorktreeRecord])
async def list_worktrees(
    repo_id: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[WorktreeService, Depends(get_worktree_service)],
) -> list[WorktreeRecord]:
    """List all worktrees for a repository."""
    return await service.list_worktrees(repo_id)


@router.delete("/{repo_id}/worktrees/{worktree_id}", status_code=204)
async def remove_worktree(
    repo_id: str,
    worktree_id: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[WorktreeService, Depends(get_worktree_service)],
) -> None:
    """Remove a worktree from disk and mark it removed."""
    try:
        await service.remove_worktree(worktree_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorktreeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/{repo_id}/worktrees/{worktree_id}/wip",
    response_model=dict,
)
async def get_wip_report(
    repo_id: str,
    worktree_id: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[WorktreeService, Depends(get_worktree_service)],
) -> dict[str, object]:
    """Return ahead/dirty_files/branch status for a worktree."""
    try:
        return await service.get_wip_report(worktree_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{repo_id}/dirty-action", status_code=204)
async def dirty_action(
    repo_id: str,
    body: DirtyAction,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[WorktreeService, Depends(get_worktree_service)],
) -> None:
    """Execute the user's chosen dirty-repo handling option."""
    try:
        await service.handle_dirty_repo(
            repo_id,
            body.option,
            commit_message=body.commit_message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorktreeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
