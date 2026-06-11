"""Per-tool handler functions for the Fleet tool dispatcher.

Each handler receives a validated Pydantic input object and a ``svcs`` dict
populated by ``dispatch_tool``. The ``svcs`` dict always contains:

  agent_svc, event_svc, workspace_svc, worktree_svc, db, evidence_svc
  _policy_svc    — PolicyService (guaranteed non-None by dispatch_tool)
  _calling_agent — AgentRecord | None (caller's record, if found)

Handlers must return a plain ``dict[str, object]``.  They may raise
``HTTPException`` or ``PolicyDenied`` — the dispatcher handles both.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Connection, text

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
from fleet.workspace.worktree_service import DirtyRepoError, OverlapError, WorktreeError

# Per-scope asyncio.Lock to serialize spawn-cap checks (P1-20 TOCTOU fix).
# Without this lock, concurrent _handle_spawn_worker calls for the same scope
# could both read the same live_count, both pass the cap check, and both spawn —
# exceeding max_live_workers.
_spawn_locks: dict[str, asyncio.Lock] = {}

# Role escalation policy: maps caller role → frozenset of roles it may spawn.
# None means unrestricted (orchestrator may spawn any role).
_SPAWN_ROLE_ALLOWLIST: dict[str, frozenset[str] | None] = {
    "orchestrator": None,  # orchestrator can spawn any role
    "worker": frozenset({"worker"}),
    "reviewer": frozenset({"worker"}),
}


async def _handle_spawn_worker(
    inp: SpawnWorkerInput, svcs: dict[str, Any]
) -> dict[str, object]:
    from fleet.api.agents import _make_backend

    agent_svc = svcs["agent_svc"]
    event_svc = svcs["event_svc"]
    policy_svc = svcs.get("_policy_svc")
    if policy_svc is None:
        raise HTTPException(
            status_code=503, detail="Policy service not configured"
        )
    calling_agent = svcs["_calling_agent"]

    # Acquire a per-scope lock to serialize the live-count check + spawn
    # (P1-20 TOCTOU fix). Without this lock, concurrent spawns for the same
    # scope all read the same live_count before any of them increments it.
    spawn_lock = _spawn_locks.setdefault(inp.scope, asyncio.Lock())
    async with spawn_lock:
        # Count live workers in this scope (for spawn rate check)
        live_workers = await agent_svc.list_agents(scope=inp.scope)
        live_count = sum(
            1 for a in live_workers if a.status not in ("archived", "failed")
        )

        # Count spawns in last 60 seconds from state_change events with action=spawn
        recent_events = await event_svc.query(
            inp.scope, type_filter="state_change", limit=500
        )
        cutoff = (
            datetime.now(UTC) - timedelta(minutes=1)
        ).isoformat().replace("+00:00", "Z")
        spawns_last_minute = sum(
            1
            for e in recent_events
            if e.ts >= cutoff and e.payload.get("action") == "spawn"
        )

        policy_svc.check_spawn_rate(
            scope=inp.scope,
            role=calling_agent.role,
            current_live_workers=live_count,
            spawns_last_minute=spawns_last_minute,
        )

        # Identity binding gap: agent_id is caller-supplied; token binding is P2-1.
        # Role escalation check: validate requested role against caller's
        # allowed spawn roles.
        # calling_agent is guaranteed non-None here: check_spawn_rate() above already
        # dereferenced calling_agent.role, so it would have raised AttributeError first.
        assert calling_agent is not None  # noqa: S101 — invariant, not user-facing
        caller_role = calling_agent.role
        allowed_spawn_roles = _SPAWN_ROLE_ALLOWLIST.get(
            caller_role, frozenset({"worker"})
        )
        if allowed_spawn_roles is not None and inp.role not in allowed_spawn_roles:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Role escalation denied: caller role '{caller_role}' cannot "
                    f"spawn role '{inp.role}'"
                ),
            )

        # Secret path guard: block any owned_path that matches a secret glob pattern.
        for path in inp.owned_paths:
            try:
                policy_svc.check_secret_path(path)
            except PolicyDenied as exc:
                raise HTTPException(status_code=403, detail=exc.reason) from exc

        record = await agent_svc.create_agent(
            scope=inp.scope,
            name=inp.name,
            role=inp.role,
            backend=_make_backend(inp.backend_type),
            model=inp.model,
            parent_id=inp.agent_id,
            repository_id=inp.repository_id,
            budget_soft_usd=inp.budget_soft_usd,
            budget_hard_usd=inp.budget_hard_usd,
            backend_name=inp.backend_type,
            task_description=inp.task_description,
        )
    agent_id = record.id

    # Create a worktree for this task when worktree_svc and task_id are available.
    worktree_svc = svcs.get("worktree_svc")
    if worktree_svc is not None and inp.task_id and inp.repository_id:
        try:
            worktree = await worktree_svc.create_worktree(
                repo_id=inp.repository_id,
                agent_id=agent_id,
                task_id=inp.task_id,
                name=inp.name,
                owned_paths=inp.owned_paths,
            )
            await agent_svc.set_worktree_id(agent_id, worktree.id)
        except (WorktreeError, DirtyRepoError, OverlapError, ValueError) as exc:
            # Non-fatal: worktree creation failure does not block agent spawn.
            # Emit an error event so the failure is audited.
            await event_svc.append(
                inp.scope,
                "error",
                f"Worktree creation failed for agent {agent_id}: {exc}",
                agent_id=agent_id,
                payload={"error": str(exc), "action": "worktree_create_failed"},
            )

    # Emit task_created state_change event (action="spawn" feeds the rate limiter).
    await event_svc.append(
        inp.scope,
        "state_change",
        "task_created",
        agent_id=agent_id,
        payload={"task_id": inp.task_id, "name": inp.name, "action": "spawn"},
    )

    return {"agent_id": agent_id, "name": record.name, "status": record.status}


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
    """Simulate merging the worktree branch into target_branch and report conflicts."""
    from fleet.review.conflict import ConflictChecker

    db = svcs["db"]

    # Look up the worktree record directly via DB to get repo path and branch.
    with db.read_connection() as conn:
        row = conn.execute(
            text(
                "SELECT w.branch, r.path AS repo_path"
                " FROM worktrees w"
                " JOIN repositories r ON w.repository_id = r.id"
                " WHERE w.id = :id"
            ),
            {"id": inp.worktree_id},
        ).fetchone()

    if row is None:
        raise ValueError(f"Worktree not found: {inp.worktree_id!r}")

    result = ConflictChecker().check(
        row.repo_path, row.branch, inp.target_branch
    )
    return {
        "worktree_id": inp.worktree_id,
        "has_conflict": result.has_conflict,
        "conflict_summary": result.summary,
        "conflict_files": result.conflict_files,
    }


async def _handle_record_validation(
    inp: RecordValidationInput, svcs: dict[str, Any]
) -> dict[str, object]:
    evidence_svc = svcs.get("evidence_svc")
    if evidence_svc is None:
        raise HTTPException(
            status_code=503, detail="evidence service not available"
        )

    row_id: int = await evidence_svc.record_evidence(
        task_id=inp.task_id,
        check_name=inp.check_name,
        status=inp.status,
        output=inp.output,
        recorded_by=inp.recorded_by if inp.recorded_by else inp.agent_id,
    )

    # Emit a review_verdict event when a reviewer records a "review" check.
    # _calling_agent is injected by dispatch_tool before handler invocation.
    calling_agent = svcs.get("_calling_agent")
    calling_role: str = calling_agent.role if calling_agent is not None else ""
    if inp.check_name == "review" and calling_role == "reviewer":
        event_svc = svcs.get("event_svc")
        if event_svc is None:
            raise HTTPException(
                status_code=503, detail="event service not available"
            )
        await event_svc.append(
            inp.scope,
            "review_verdict",
            f"reviewer verdict: {inp.status}",
            agent_id=inp.agent_id,
            payload={
                "task_id": inp.task_id,
                "verdict": inp.status,
                "output": inp.output,
            },
        )

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


async def _handle_execute_merge(
    inp: ExecuteMergeInput, svcs: dict[str, Any]
) -> dict[str, object]:
    """Execute a squash merge via MergeService.

    Requires the merge service to be wired into svcs["merge_svc"].
    Returns {"commit_sha", "branch", "task_id"} on success.
    """
    from fleet.review.merge import ConflictError, MergeGateError

    merge_svc = svcs.get("merge_svc")
    if merge_svc is None:
        raise HTTPException(
            status_code=503, detail="merge service not available"
        )

    agent_id = inp.agent_id

    try:
        result = await merge_svc.execute_merge(
            worktree_id=inp.worktree_id,
            agent_id=agent_id,
            scope=inp.scope,
        )
    except MergeGateError as exc:
        raise HTTPException(
            status_code=422, detail=f"merge gate: {exc.reason}"
        ) from exc
    except ConflictError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"merge gate: {exc.conflict_result.summary[:500]}",
        ) from exc
    return {
        "commit_sha": result.commit_sha,
        "branch": result.branch,
        "task_id": result.task_id,
    }
