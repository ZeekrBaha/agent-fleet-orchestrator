"""FastAPI router for Fleet tool endpoints (Task 4.1).

Exposes POST /api/tools/{tool_name} — one route that dispatches to the correct
handler via a registry dict.  All tool inputs are validated by Pydantic before
any service call; invalid input → 422 (never 500).

Each handler:
  1. Emits a ``tool_call`` event (payload scrubbed of secret values).
  2. Executes the operation.
  3. Emits a ``tool_result`` (or ``tool_result_error``) event.
  4. Returns a result dict.

Dependency injection follows the same module-level state-holder pattern used by
fleet/api/agents.py and fleet/api/workspaces.py.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Connection, text

from fleet.api.auth import require_token

router = APIRouter(prefix="/api/tools", tags=["tools"])

# ---------------------------------------------------------------------------
# Dependency injection state (set once at startup)
# ---------------------------------------------------------------------------

_services: dict[str, Any] | None = None


def set_tool_services(
    agent_svc: Any,
    event_svc: Any,
    workspace_svc: Any,
    worktree_svc: Any,
    db: Any,
) -> None:
    """Wire service instances into this module (called at application startup)."""
    global _services
    _services = {
        "agent_svc": agent_svc,
        "event_svc": event_svc,
        "workspace_svc": workspace_svc,
        "worktree_svc": worktree_svc,
        "db": db,
    }


def get_tool_services() -> dict[str, Any]:
    """Return the injected services dict; raises if not yet configured."""
    if _services is None:
        raise RuntimeError(
            "Tool services not initialised — call set_tool_services() first"
        )
    return _services


# ---------------------------------------------------------------------------
# Tool input schemas — all include agent_id and scope
# ---------------------------------------------------------------------------


class SpawnWorkerInput(BaseModel):
    agent_id: str
    scope: str
    name: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1)
    task_description: str
    model: str = Field(default="claude-sonnet-4-6")
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
    command: str = Field(min_length=1, max_length=1024)
    exit_code: int
    summary: str = Field(max_length=4096)
    skipped: str | None = None
    residual_risk: str | None = None


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


# ---------------------------------------------------------------------------
# Secret scrubbing — remove values whose keys match secret patterns
# ---------------------------------------------------------------------------

_SECRET_KEY_PATTERN = re.compile(
    r"(token|secret|password|api_key|auth)", re.IGNORECASE
)


def _scrub_payload(payload: dict[str, object]) -> dict[str, object]:
    """Return a copy of payload with secret-looking values replaced by '[REDACTED]'."""
    scrubbed: dict[str, object] = {}
    for k, v in payload.items():
        if _SECRET_KEY_PATTERN.search(k):
            scrubbed[k] = "[REDACTED]"
        else:
            scrubbed[k] = v
    return scrubbed


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


async def _emit_tool_call(
    event_svc: Any,
    scope: str,
    agent_id: str,
    tool_name: str,
    input_dict: dict[str, object],
) -> None:
    await event_svc.append(
        scope,
        "tool_call",
        f"Tool call: {tool_name}",
        agent_id=agent_id,
        payload={"tool_name": tool_name, "input": _scrub_payload(input_dict)},
    )


async def _emit_tool_result(
    event_svc: Any,
    scope: str,
    agent_id: str,
    tool_name: str,
    result: dict[str, object],
) -> None:
    await event_svc.append(
        scope,
        "tool_result",
        f"Tool result: {tool_name}",
        agent_id=agent_id,
        payload={"tool_name": tool_name, "result": result},
    )


async def _emit_tool_error(
    event_svc: Any,
    scope: str,
    agent_id: str,
    tool_name: str,
    error: str,
) -> None:
    await event_svc.append(
        scope,
        "tool_result",
        f"Tool error: {tool_name}",
        agent_id=agent_id,
        payload={"tool_name": tool_name, "error": error},
    )


# ---------------------------------------------------------------------------
# Per-tool handlers
# ---------------------------------------------------------------------------


async def _handle_spawn_worker(
    inp: SpawnWorkerInput, svcs: dict[str, Any]
) -> dict[str, object]:
    from fleet.agents.backends.mock import MockBackend

    agent_svc = svcs["agent_svc"]
    record = await agent_svc.create_agent(
        scope=inp.scope,
        name=inp.name,
        role=inp.role,
        backend=MockBackend(transcript=[]),
        model=inp.model,
        parent_id=inp.agent_id,
        repository_id=inp.repository_id,
        budget_soft_usd=inp.budget_soft_usd,
        budget_hard_usd=inp.budget_hard_usd,
        backend_name="mock",
    )
    return {"agent_id": record.id, "name": record.name, "status": record.status}


async def _handle_send_message(
    inp: SendMessageInput, svcs: dict[str, Any]
) -> dict[str, object]:
    agent_svc = svcs["agent_svc"]
    inbox_id = await agent_svc.send_message(
        inp.target_agent_id, inp.agent_id, inp.message
    )
    return {"inbox_id": inbox_id}


async def _handle_list_agents(
    inp: ListAgentsInput, svcs: dict[str, Any]
) -> dict[str, object]:
    agent_svc = svcs["agent_svc"]
    agents = await agent_svc.list_agents(inp.scope)
    return {
        "agents": [
            {"id": a.id, "name": a.name, "status": a.status} for a in agents
        ]
    }


async def _handle_get_agent_logs(
    inp: GetAgentLogsInput, svcs: dict[str, Any]
) -> dict[str, object]:
    event_svc = svcs["event_svc"]
    events = await event_svc.query(
        inp.scope, agent_id=inp.target_agent_id, limit=inp.limit
    )
    return {
        "events": [
            {"id": e.id, "type": e.type, "summary": e.summary, "ts": e.ts}
            for e in events
        ]
    }


async def _handle_stop_agent(
    inp: StopAgentInput, svcs: dict[str, Any]
) -> dict[str, object]:
    agent_svc = svcs["agent_svc"]
    await agent_svc.archive_agent(inp.target_agent_id)
    return {"status": "archived", "agent_id": inp.target_agent_id}


async def _handle_worker_wip(
    inp: WorkerWipInput, svcs: dict[str, Any]
) -> dict[str, object]:
    """Look up the agent's worktree_id, then return its WIP report."""
    agent_svc = svcs["agent_svc"]
    worktree_svc = svcs["worktree_svc"]

    record = await agent_svc.get_agent(inp.target_agent_id)
    if record is None or record.worktree_id is None:
        return {"error": f"Agent {inp.target_agent_id!r} has no worktree"}

    wip: dict[str, object] = await worktree_svc.get_wip_report(record.worktree_id)
    return wip


async def _handle_check_conflict(
    inp: CheckConflictInput, svcs: dict[str, Any]
) -> dict[str, object]:
    worktree_svc = svcs["worktree_svc"]
    # Delegate to worktree service; simulate_merge is done via get_wip_report path.
    # We return the current WIP status as a conflict indicator.
    try:
        wip: dict[str, object] = await worktree_svc.get_wip_report(inp.worktree_id)
        return {
            "worktree_id": inp.worktree_id,
            "target_branch": inp.target_branch,
            "wip": wip,
        }
    except ValueError as exc:
        return {"error": str(exc)}


async def _handle_record_validation(
    inp: RecordValidationInput, svcs: dict[str, Any]
) -> dict[str, object]:
    db = svcs["db"]
    ts = datetime.now(UTC).isoformat()

    def _write(conn: Connection) -> int:
        result = conn.execute(
            text(
                "INSERT INTO validation_evidence"
                " (task_id, command, exit_code, summary, skipped, residual_risk, ts)"
                " VALUES (:task_id, :command, :exit_code, :summary,"
                "         :skipped, :residual_risk, :ts)"
            ),
            {
                "task_id": inp.task_id,
                "command": inp.command,
                "exit_code": inp.exit_code,
                "summary": inp.summary,
                "skipped": inp.skipped,
                "residual_risk": inp.residual_risk,
                "ts": ts,
            },
        )
        conn.commit()
        last_id = result.lastrowid
        if last_id is None:
            raise RuntimeError("INSERT did not return a rowid")
        return int(last_id)

    row_id: int = await db.write(_write)
    return {"id": row_id, "task_id": inp.task_id, "recorded": True}


async def _handle_report_issue(
    inp: ReportIssueInput, svcs: dict[str, Any]
) -> dict[str, object]:
    event_svc = svcs["event_svc"]
    event_id = await event_svc.append(
        inp.scope,
        "error",
        inp.title,
        agent_id=inp.agent_id,
        payload={"severity": inp.severity, "description": inp.description},
    )
    return {"event_id": event_id, "severity": inp.severity}


async def _handle_update_progress(
    inp: UpdateProgressInput, svcs: dict[str, Any]
) -> dict[str, object]:
    event_svc = svcs["event_svc"]
    event_id = await event_svc.append(
        inp.scope,
        "state_change",
        inp.message,
        agent_id=inp.agent_id,
        payload={"percent": inp.percent, "message": inp.message},
    )
    return {"event_id": event_id}


async def _handle_request_approval(
    inp: RequestApprovalInput, svcs: dict[str, Any]
) -> dict[str, object]:
    db = svcs["db"]
    approval_id = str(uuid.uuid4())
    ts = datetime.now(UTC).isoformat()

    def _write(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO approvals"
                " (id, scope, requester_agent_id, operation, rationale, risk,"
                "  status, created_at)"
                " VALUES (:id, :scope, :requester_agent_id, :operation,"
                "         :rationale, :risk, 'pending', :created_at)"
            ),
            {
                "id": approval_id,
                "scope": inp.scope,
                "requester_agent_id": inp.agent_id,
                "operation": inp.operation,
                "rationale": inp.rationale,
                "risk": inp.risk,
                "created_at": ts,
            },
        )
        conn.commit()

    await db.write(_write)
    return {"id": approval_id, "status": "pending"}


async def _handle_memory_write(
    inp: MemoryWriteInput, svcs: dict[str, Any]
) -> dict[str, object]:
    db = svcs["db"]
    mem_id = str(uuid.uuid4())
    ts = datetime.now(UTC).isoformat()

    def _write(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO memory"
                " (id, scope, kind, title, body, created_at)"
                " VALUES (:id, :scope, :kind, :title, :body, :created_at)"
            ),
            {
                "id": mem_id,
                "scope": inp.scope,
                "kind": inp.kind,
                "title": inp.title,
                "body": inp.body,
                "created_at": ts,
            },
        )
        conn.commit()

    await db.write(_write)
    return {"id": mem_id, "kind": inp.kind}


# ---------------------------------------------------------------------------
# Tool registry — maps tool_name -> (InputSchema, handler_fn)
# ---------------------------------------------------------------------------

_TOOL_REGISTRY: dict[str, tuple[type[BaseModel], Any]] = {
    "spawn_worker": (SpawnWorkerInput, _handle_spawn_worker),
    "send_message": (SendMessageInput, _handle_send_message),
    "list_agents": (ListAgentsInput, _handle_list_agents),
    "get_agent_logs": (GetAgentLogsInput, _handle_get_agent_logs),
    "stop_agent": (StopAgentInput, _handle_stop_agent),
    "worker_wip": (WorkerWipInput, _handle_worker_wip),
    "check_conflict": (CheckConflictInput, _handle_check_conflict),
    "record_validation": (RecordValidationInput, _handle_record_validation),
    "report_issue": (ReportIssueInput, _handle_report_issue),
    "update_progress": (UpdateProgressInput, _handle_update_progress),
    "request_approval": (RequestApprovalInput, _handle_request_approval),
    "memory_write": (MemoryWriteInput, _handle_memory_write),
}


# ---------------------------------------------------------------------------
# Single dispatcher route
# ---------------------------------------------------------------------------


@router.post("/{tool_name}")
async def dispatch_tool(
    tool_name: str,
    body: dict[str, object],
    _auth: Annotated[None, Depends(require_token)],
) -> dict[str, object]:
    """Dispatch a tool call by name.

    Validates input against the tool's Pydantic schema → 422 on bad input.
    Returns 404 for unknown tool names.
    Emits tool_call + tool_result audit events around each execution.
    """
    entry = _TOOL_REGISTRY.get(tool_name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {tool_name!r}")

    input_schema, handler = entry

    # Validate input — Pydantic ValidationError → 422 Unprocessable Entity
    try:
        validated = input_schema.model_validate(body)
    except ValidationError as exc:
        # Re-raise as FastAPI RequestValidationError so the framework returns 422
        raise RequestValidationError(errors=exc.errors()) from exc
    agent_id: str = str(getattr(validated, "agent_id", ""))
    scope: str = str(getattr(validated, "scope", ""))

    svcs = get_tool_services()
    event_svc = svcs["event_svc"]

    # Emit tool_call audit event (scrubbed)
    await _emit_tool_call(
        event_svc, scope, agent_id, tool_name, validated.model_dump()
    )

    try:
        result = await handler(validated, svcs)
    except HTTPException:
        # Re-raise HTTP exceptions from handlers unchanged
        raise
    except Exception as exc:
        # Map unexpected errors to audit event + 500
        # Typed: ValueError → 400, everything else → 500
        error_msg = str(exc)
        await _emit_tool_error(event_svc, scope, agent_id, tool_name, error_msg)
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=error_msg) from exc
        raise HTTPException(
            status_code=500, detail=f"Tool execution failed: {error_msg}"
        ) from exc

    await _emit_tool_result(event_svc, scope, agent_id, tool_name, result)
    result_typed: dict[str, object] = result
    return result_typed
