"""FastAPI router for Fleet tool endpoints (Task 4.1 + 4.2).

Exposes POST /api/tools/{tool_name} — one route that dispatches to the correct
handler via a registry dict.  All tool inputs are validated by Pydantic before
any service call; invalid input → 422 (never 500).

Each handler:
  1. Emits a ``tool_call`` event (payload scrubbed of secret values).
  2. Checks policy (role-based ACL — fail-closed per ADR-005).
  3. Executes the operation.
  4. Emits a ``tool_result`` (or ``tool_result_error``) event.
  5. Returns a result dict.

Dependency injection follows the same module-level state-holder pattern used by
fleet/api/agents.py and fleet/api/workspaces.py.

Handler implementations live in ``fleet/api/tool_handlers.py``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from fleet.api.auth import AgentIdentity, require_agent_identity
from fleet.api.tool_handlers import (
    _handle_check_conflict,
    _handle_execute_merge,
    _handle_get_agent_logs,
    _handle_list_agents,
    _handle_memory_write,
    _handle_record_validation,
    _handle_report_issue,
    _handle_request_approval,
    _handle_send_message,
    _handle_spawn_worker,
    _handle_stop_agent,
    _handle_update_progress,
    _handle_worker_wip,
)
from fleet.api.tool_schemas import (
    CheckConflictInput,
    ExecuteMergeInput,
    GetAgentLogsInput,
    ListAgentsInput,
    MemoryWriteInput,
    RecordValidationInput,
    ReportIssueInput,
    RequestApprovalInput,
    SendMessageInput,
    SpawnWorkerInput,
    StopAgentInput,
    UpdateProgressInput,
    WorkerWipInput,
)
from fleet.policy.service import PolicyDenied

router = APIRouter(prefix="/api/tools", tags=["tools"])

# ---------------------------------------------------------------------------
# Dependency injection state (set once at startup)
# ---------------------------------------------------------------------------

_services: dict[str, Any] | None = None

# Policy service — injected separately so it can be tested in isolation
_policy_svc: Any = None


def set_tool_services(
    agent_svc: Any,
    event_svc: Any,
    workspace_svc: Any,
    worktree_svc: Any,
    db: Any,
    evidence_svc: Any = None,
    merge_svc: Any = None,
) -> None:
    """Wire service instances into this module (called at application startup).

    Resets the policy service to None — callers MUST call set_policy_service()
    after this, otherwise all tool calls will be rejected with 503.
    """
    global _services, _policy_svc
    _services = {
        "agent_svc": agent_svc,
        "event_svc": event_svc,
        "workspace_svc": workspace_svc,
        "worktree_svc": worktree_svc,
        "db": db,
        "evidence_svc": evidence_svc,
        "merge_svc": merge_svc,
    }
    _policy_svc = None


def get_tool_services() -> dict[str, Any]:
    """Return the injected services dict; raises if not yet configured."""
    if _services is None:
        raise RuntimeError(
            "Tool services not initialised — call set_tool_services() first"
        )
    return _services


def set_policy_service(policy_svc: Any) -> None:
    """Wire the PolicyService instance into this module."""
    global _policy_svc
    _policy_svc = policy_svc


def get_policy_service() -> Any:
    """Return the injected PolicyService.

    Returns None when not configured — callers must check and raise 503.
    """
    return _policy_svc


# ---------------------------------------------------------------------------
# Secret scrubbing — remove values whose keys match secret patterns
# ---------------------------------------------------------------------------

_SECRET_KEY_PATTERN = re.compile(
    r"(token|secret|password|api_key|auth)", re.IGNORECASE
)


def _scrub_payload(payload: dict[str, object]) -> dict[str, object]:
    """Return a copy of payload with secret-looking values replaced by '[REDACTED]'.

    Only key-name scrubbing is implemented; value-pattern scrubbing
    (e.g. bearer tokens in message bodies) is out of scope for MVP.
    """
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
        "tool_result_error",
        f"Tool error: {tool_name}",
        agent_id=agent_id,
        payload={"tool_name": tool_name, "error": error},
    )


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
    "execute_merge": (ExecuteMergeInput, _handle_execute_merge),
}


# ---------------------------------------------------------------------------
# Single dispatcher route
# ---------------------------------------------------------------------------


@router.post("/{tool_name}")
async def dispatch_tool(
    tool_name: str,
    body: dict[str, object],
    agent_identity: Annotated[AgentIdentity, Depends(require_agent_identity)],
) -> dict[str, object]:
    """Dispatch a tool call by name.

    Validates input against the tool's Pydantic schema → 422 on bad input.
    Returns 404 for unknown tool names.
    Emits tool_call + tool_result audit events around each execution.

    Auth: accepts per-agent tokens (FLEET_AGENT_TOKEN) or FLEET_API_TOKEN
    (admin/impersonation).  Per-agent path: if body ``agent_id`` is present
    and disagrees with the authenticated identity → 403.
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

    body_agent_id: str = str(getattr(validated, "agent_id", ""))
    scope: str = str(getattr(validated, "scope", ""))

    svcs = get_tool_services()
    event_svc = svcs["event_svc"]
    agent_svc = svcs.get("agent_svc")

    # --- Resolve canonical agent_id and role ---------------------------------
    if agent_identity.is_admin:
        # Admin/impersonation: use body agent_id.
        # Resolve role: try agent_svc first (works in tests with mocked services),
        # then fall back to direct DB lookup (works when agent_svc returns None).
        agent_id = body_agent_id
        calling_agent: Any = None
        calling_role: str = "unknown"
        if agent_svc is not None and agent_id:
            calling_agent = await agent_svc.get_agent(agent_id)
            if calling_agent is not None:
                calling_role = calling_agent.role
        if calling_role == "unknown":
            db_svc = svcs.get("db")
            if db_svc is not None and agent_id:
                with db_svc.read_connection() as _conn:
                    from sqlalchemy import text as _text  # noqa: PLC0415
                    _row = _conn.execute(
                        _text("SELECT role FROM agents WHERE id = :id"),
                        {"id": agent_id},
                    ).fetchone()
                    if _row is not None:
                        calling_role = str(_row[0])
        # Emit admin impersonation event so the audit trail is explicit.
        await event_svc.append(
            scope,
            "admin_impersonation",
            f"Admin impersonating agent {agent_id!r} for tool {tool_name!r}",
            agent_id=agent_id,
            payload={"tool_name": tool_name, "impersonated_id": agent_id},
        )
    else:
        # Per-agent token: authenticated identity IS the caller.
        # Reject if body agent_id is present and disagrees.
        if body_agent_id and body_agent_id != agent_identity.agent_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "type": "identity_mismatch",
                    "title": "Identity mismatch",
                    "detail": (
                        f"Token authenticates {agent_identity.agent_id!r} "
                        f"but body claims {body_agent_id!r}"
                    ),
                    "status": 403,
                },
            )
        agent_id = agent_identity.agent_id or body_agent_id
        calling_role = agent_identity.role or "unknown"
        calling_agent = None
        if agent_svc is not None and agent_id:
            calling_agent = await agent_svc.get_agent(agent_id)
    # -------------------------------------------------------------------------

    # Emit tool_call audit event (scrubbed)
    await _emit_tool_call(
        event_svc, scope, agent_id, tool_name, validated.model_dump()
    )

    # --- Policy check (ADR-005: fail-closed) ---------------------------------
    policy_svc = get_policy_service()
    if policy_svc is None:
        raise HTTPException(
            status_code=503,
            detail="Policy service not configured",
        )

    try:
        policy_svc.check_tool_allowed(role=calling_role, tool_name=tool_name)
    except PolicyDenied as exc:
        await _emit_tool_error(event_svc, scope, agent_id, tool_name, exc.reason)
        raise HTTPException(
            status_code=403,
            detail={
                "type": "policy_denied",
                "title": "Tool access denied",
                "detail": exc.reason,
                "status": 403,
            },
        ) from exc
    # -------------------------------------------------------------------------

    # Pass policy context to handlers that need it (spawn_worker rate-limiting).
    svcs_with_ctx = {
        **svcs,
        "_policy_svc": policy_svc,
        "_calling_agent": calling_agent,
    }

    try:
        result = await handler(validated, svcs_with_ctx)
    except HTTPException:
        # Re-raise HTTP exceptions from handlers unchanged
        raise
    except PolicyDenied as exc:
        # Spawn rate exceeded → 429 Too Many Requests (RFC 7807 body)
        await _emit_tool_error(event_svc, scope, agent_id, tool_name, exc.reason)
        raise HTTPException(
            status_code=429,
            detail={
                "type": "spawn_rate_exceeded",
                "title": "Spawn rate limit exceeded",
                "detail": exc.reason,
                "status": 429,
            },
        ) from exc
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        # Never swallow process-level signals or task cancellation.
        raise
    except Exception as exc:
        # Typed mapping: ValueError → 400, Exception → 500
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
