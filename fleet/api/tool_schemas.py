"""Pydantic input schemas for Fleet tool endpoints.

All schemas include ``agent_id`` and ``scope`` fields.  Separated from
``fleet/api/tools.py`` to keep that module under 500 lines.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SpawnWorkerInput(BaseModel):
    agent_id: str
    scope: str
    name: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1)
    task_description: str
    task_id: str | None = None
    model: str = Field(default="claude-sonnet-4-6")
    backend_type: Literal["mock", "claude"] = "mock"
    repository_id: str | None = None
    owned_paths: list[str] = Field(default_factory=list)
    budget_soft_usd: float | None = None
    budget_hard_usd: float | None = None


class SendMessageInput(BaseModel):
    agent_id: str
    scope: str
    target_agent_id: str
    message: str = Field(min_length=1, max_length=32768)


class ListAgentsInput(BaseModel):
    agent_id: str
    scope: str


class GetAgentLogsInput(BaseModel):
    agent_id: str
    scope: str
    target_agent_id: str
    limit: int = Field(default=50, ge=1, le=500)


class StopAgentInput(BaseModel):
    agent_id: str
    scope: str
    target_agent_id: str
    reason: str = Field(default="", max_length=256)


class WorkerWipInput(BaseModel):
    agent_id: str
    scope: str
    target_agent_id: str


class CheckConflictInput(BaseModel):
    agent_id: str
    scope: str
    worktree_id: str
    target_branch: str = Field(default="main")


class RecordValidationInput(BaseModel):
    agent_id: str
    scope: str
    task_id: str
    check_name: str = Field(min_length=1, max_length=1024)
    status: Literal["pass", "fail", "skip"]
    output: str = Field(default="", max_length=65536)
    recorded_by: str | None = None


class ReportIssueInput(BaseModel):
    agent_id: str
    scope: str
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(max_length=8192)
    severity: str = Field(default="info", pattern=r"^(info|warning|error|critical)$")


class UpdateProgressInput(BaseModel):
    agent_id: str
    scope: str
    message: str = Field(min_length=1, max_length=1024)
    percent: int | None = Field(default=None, ge=0, le=100)


class RequestApprovalInput(BaseModel):
    agent_id: str
    scope: str
    operation: str = Field(min_length=1, max_length=256)
    rationale: str = Field(min_length=1, max_length=2048)
    risk: str = Field(max_length=1024)


class MemoryWriteInput(BaseModel):
    agent_id: str
    scope: str
    kind: str = Field(
        pattern=r"^(architecture_decision|known_bug|failed_attempt|command_recipe|dependency_note|deployment_note)$"
    )
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=16384)


class ExecuteMergeInput(BaseModel):
    agent_id: str
    scope: str
    worktree_id: str = Field(min_length=1)
