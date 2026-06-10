"""Fleet Pydantic v2 domain models.

These are plain BaseModel classes (no ORM mapping).
Field names and types mirror the SQLite schema in design.md.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Status enumerations
# ---------------------------------------------------------------------------

AgentStatus = Literal[
    "idle", "running", "waiting", "paused_budget", "failed", "archived"
]
WorktreeStatus = Literal["active", "merged", "removed"]
InboxStatus = Literal["pending", "delivered", "failed"]
ApprovalStatus = Literal["pending", "approved", "denied"]
MemoryKind = Literal[
    "architecture_decision",
    "known_bug",
    "failed_attempt",
    "command_recipe",
    "dependency_note",
    "deployment_note",
]

# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """Mirrors the `events` table (append-only audit log)."""

    id: int | None = None
    ts: str
    scope: str
    agent_id: str | None = None
    type: str
    summary: str
    payload: dict  # type: ignore[type-arg]


class AgentRecord(BaseModel):
    """Mirrors the `agents` table."""

    id: str
    name: str
    scope: str
    role: str
    backend: str
    model: str
    status: AgentStatus
    parent_id: str | None = None
    repository_id: str | None = None
    session_ref: str | None = None
    worktree_id: str | None = None
    context_pct: float = 0.0
    cost_usd: float = 0.0
    budget_soft_usd: float | None = None
    budget_hard_usd: float | None = None
    created_at: str
    updated_at: str


class WorktreeRecord(BaseModel):
    """Mirrors the `worktrees` table."""

    id: str
    agent_id: str
    repository_id: str
    path: str
    branch: str
    base_branch: str
    owned_paths_json: str
    status: WorktreeStatus
    created_at: str


class InboxMessage(BaseModel):
    """Mirrors the `inbox` table (FIFO per to_agent_id)."""

    id: int | None = None
    to_agent_id: str
    sender: str
    message: str
    status: InboxStatus
    created_at: str
    delivered_at: str | None = None


class ApprovalRecord(BaseModel):
    """Mirrors the `approvals` table."""

    id: str
    scope: str
    requester_agent_id: str
    operation: str
    rationale: str
    risk: str
    status: ApprovalStatus
    decided_by: str | None = None
    comment: str | None = None
    created_at: str
    decided_at: str | None = None
