"""FastAPI router for agent lifecycle management (Task 2.2).

Endpoints:
    POST   /api/agents                    — create agent
    GET    /api/agents                    — list agents (by scope)
    GET    /api/agents/{agent_id}         — get one agent
    POST   /api/agents/{agent_id}/messages — send message
    POST   /api/agents/{agent_id}/interrupt — interrupt agent
    DELETE /api/agents/{agent_id}         — archive agent
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from fleet.agents.backends.mock import MockBackend
from fleet.agents.backends.protocol import AgentBackend
from fleet.agents.service import AgentService
from fleet.api.auth import require_token
from fleet.models import AgentRecord

router = APIRouter(prefix="/api/agents", tags=["agents"])

# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

_agent_service: AgentService | None = None


def get_agent_service() -> AgentService:
    """Dependency: return the active AgentService or raise RuntimeError."""
    if _agent_service is None:
        raise RuntimeError("AgentService not initialized")
    return _agent_service


def set_agent_service(svc: AgentService) -> None:
    """Set the global AgentService instance (called during app startup)."""
    global _agent_service
    _agent_service = svc


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

def _make_backend(backend_type: str) -> AgentBackend:
    """Create a backend instance by type name.

    Supported types:
        - "mock" — deterministic MockBackend with no transcript (idle loop)

    Task 2.3 will add the "claude" / "anthropic" adapter.
    """
    if backend_type == "mock":
        return MockBackend(transcript=[])
    raise ValueError(f"Unknown backend type: {backend_type!r}")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AgentCreate(BaseModel):
    """Request body for POST /api/agents."""

    scope: str
    name: str
    role: str
    model: str
    backend_type: str = "mock"
    parent_id: str | None = None
    repository_id: str | None = None
    budget_soft_usd: float | None = None
    budget_hard_usd: float | None = None


class SendMessage(BaseModel):
    """Request body for POST /api/agents/{agent_id}/messages."""

    sender: str
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=AgentRecord, status_code=201)
async def create_agent(
    body: AgentCreate,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> AgentRecord:
    """Create a new agent and start its session."""
    backend = _make_backend(body.backend_type)
    return await service.create_agent(
        scope=body.scope,
        name=body.name,
        role=body.role,
        backend=backend,
        model=body.model,
        parent_id=body.parent_id,
        repository_id=body.repository_id,
        budget_soft_usd=body.budget_soft_usd,
        budget_hard_usd=body.budget_hard_usd,
        backend_name=body.backend_type,
    )


@router.get("", response_model=list[AgentRecord])
async def list_agents(
    scope: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> list[AgentRecord]:
    """List all non-archived agents in scope."""
    return await service.list_agents(scope)


@router.get("/{agent_id}", response_model=AgentRecord)
async def get_agent(
    agent_id: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> AgentRecord:
    """Get a single agent by id."""
    record = await service.get_agent(agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return record


@router.post("/{agent_id}/messages", response_model=dict)
async def send_message(
    agent_id: str,
    body: SendMessage,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> dict[str, int]:
    """Send a message to an agent's inbox. Returns the inbox id."""
    record = await service.get_agent(agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    inbox_id = await service.send_message(agent_id, body.sender, body.message)
    return {"inbox_id": inbox_id}


@router.post("/{agent_id}/interrupt", status_code=204)
async def interrupt_agent(
    agent_id: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> None:
    """Interrupt the current turn for an agent."""
    record = await service.get_agent(agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await service.interrupt_agent(agent_id)


@router.delete("/{agent_id}", status_code=204)
async def archive_agent(
    agent_id: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> None:
    """Archive an agent and stop its session."""
    record = await service.get_agent(agent_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await service.archive_agent(agent_id)
