"""FastAPI router for the Fleet web dashboard (Task 7.1).

Routes:
    GET /dashboard/                                 — Agent Roster
    GET /dashboard/agents/{agent_id}/conversation  — Agent Conversation
    GET /dashboard/timeline                        — Event Timeline
    GET /dashboard/worktrees/{worktree_id}         — Worktree / Diff
    GET /dashboard/tasks/{task_id}/validation      — Validation & Merge
    GET /dashboard/approvals                       — Approval Queue
    POST /dashboard/approvals/{approval_id}/decide — Inline approval decision (htmx)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from fleet.approvals.service import ApprovalService
from fleet.db import DatabaseManager

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Module-level singletons set during app startup
_db: DatabaseManager | None = None
_templates: Jinja2Templates | None = None
_approval_svc: ApprovalService | None = None


def get_db() -> DatabaseManager:
    """Return the active DatabaseManager or raise RuntimeError."""
    if _db is None:
        raise RuntimeError("Dashboard DatabaseManager not initialized")
    return _db


def get_templates() -> Jinja2Templates:
    """Return the active Jinja2Templates instance or raise RuntimeError."""
    if _templates is None:
        raise RuntimeError("Dashboard Jinja2Templates not initialized")
    return _templates


def set_db(db: DatabaseManager) -> None:
    """Set the global DatabaseManager (called during app startup)."""
    global _db
    _db = db


def set_templates(templates: Jinja2Templates) -> None:
    """Set the global Jinja2Templates instance (called during app startup)."""
    global _templates
    _templates = templates


def set_approval_service(svc: ApprovalService) -> None:
    """Set the global ApprovalService (called during app startup)."""
    global _approval_svc
    _approval_svc = svc


# ---------------------------------------------------------------------------
# Template helpers (registered as Jinja2 globals)
# ---------------------------------------------------------------------------


def _relative_time(ts_str: str | None) -> str:
    """Return a human-readable relative time string from an ISO timestamp."""
    if not ts_str:
        return "—"
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        delta = now - ts
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"
    except ValueError:
        return ts_str


def _format_cost(cost: float | None) -> str:
    """Format a cost_usd value for display."""
    if cost is None:
        return "$0.000"
    return f"${cost:.3f}"


def _format_context_pct(pct: float | None) -> str:
    """Format context percentage."""
    if pct is None:
        return "0%"
    return f"{pct:.1f}%"


# ---------------------------------------------------------------------------
# Shared query helpers
# ---------------------------------------------------------------------------


def _query_agents(scope: str | None = None) -> list[dict[str, Any]]:
    """Fetch agents from DB, optionally filtered by scope."""
    db = get_db()
    with db.read_connection() as conn:
        if scope:
            rows = conn.execute(
                text(
                    "SELECT id, name, scope, role, backend, model, status,"
                    " context_pct, cost_usd, budget_soft_usd, budget_hard_usd,"
                    " worktree_id, updated_at, created_at"
                    " FROM agents WHERE scope = :scope ORDER BY created_at DESC"
                ),
                {"scope": scope},
            ).fetchall()
        else:
            rows = conn.execute(
                text(
                    "SELECT id, name, scope, role, backend, model, status,"
                    " context_pct, cost_usd, budget_soft_usd, budget_hard_usd,"
                    " worktree_id, updated_at, created_at"
                    " FROM agents ORDER BY created_at DESC"
                )
            ).fetchall()
    return [dict(r._mapping) for r in rows]


def _query_one_agent(agent_id: str) -> dict[str, Any] | None:
    """Fetch a single agent by id."""
    db = get_db()
    with db.read_connection() as conn:
        row = conn.execute(
            text(
                "SELECT id, name, scope, role, backend, model, status,"
                " context_pct, cost_usd, budget_soft_usd, budget_hard_usd,"
                " worktree_id, updated_at, created_at"
                " FROM agents WHERE id = :id"
            ),
            {"id": agent_id},
        ).fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def _query_events(
    scope: str | None = None,
    agent_id: str | None = None,
    type_filter: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Fetch events, optionally filtered."""
    db = get_db()
    clauses = []
    params: dict[str, Any] = {}
    if scope:
        clauses.append("scope = :scope")
        params["scope"] = scope
    if agent_id:
        clauses.append("agent_id = :agent_id")
        params["agent_id"] = agent_id
    if type_filter:
        clauses.append("type = :type_filter")
        params["type_filter"] = type_filter
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.read_connection() as conn:
        rows = conn.execute(
            text(
                f"SELECT id, ts, scope, agent_id, type, summary, payload_json"
                f" FROM events {where} ORDER BY id DESC LIMIT :limit"
            ),
            {**params, "limit": limit},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def _query_one_worktree(worktree_id: str) -> dict[str, Any] | None:
    """Fetch a single worktree by id."""
    db = get_db()
    with db.read_connection() as conn:
        row = conn.execute(
            text(
                "SELECT id, agent_id, repository_id, task_id, path, branch,"
                " base_branch, owned_paths_json, status, created_at"
                " FROM worktrees WHERE id = :id"
            ),
            {"id": worktree_id},
        ).fetchone()
    if row is None:
        return None
    return dict(row._mapping)


def _query_task_with_evidence(task_id: str) -> dict[str, Any] | None:
    """Fetch a task and its validation evidence."""
    db = get_db()
    with db.read_connection() as conn:
        task_row = conn.execute(
            text("SELECT * FROM tasks WHERE id = :id"),
            {"id": task_id},
        ).fetchone()
        if task_row is None:
            return None
        evidence_rows = conn.execute(
            text(
                "SELECT id, task_id, check_name, status, output, recorded_by, ts"
                " FROM validation_evidence WHERE task_id = :task_id ORDER BY id"
            ),
            {"task_id": task_id},
        ).fetchall()
    task = dict(task_row._mapping)
    task["evidence"] = [dict(r._mapping) for r in evidence_rows]
    return task


def _query_approvals(scope: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Fetch pending and decided approvals separately."""
    db = get_db()
    clauses_pending = ["status = 'pending'"]
    clauses_decided = ["status != 'pending'"]
    params: dict[str, Any] = {}
    if scope:
        clauses_pending.append("scope = :scope")
        clauses_decided.append("scope = :scope")
        params["scope"] = scope
    with db.read_connection() as conn:
        pending_rows = conn.execute(
            text(
                "SELECT id, scope, requester_agent_id, operation, rationale, risk,"
                " status, decided_by, comment, created_at, decided_at"
                " FROM approvals WHERE "
                + " AND ".join(clauses_pending)
                + " ORDER BY created_at DESC"
            ),
            params,
        ).fetchall()
        decided_rows = conn.execute(
            text(
                "SELECT id, scope, requester_agent_id, operation, rationale, risk,"
                " status, decided_by, comment, created_at, decided_at"
                " FROM approvals WHERE "
                + " AND ".join(clauses_decided)
                + " ORDER BY decided_at DESC LIMIT 50"
            ),
            params,
        ).fetchall()
    return {
        "pending": [dict(r._mapping) for r in pending_rows],
        "decided": [dict(r._mapping) for r in decided_rows],
    }


def _get_scopes() -> list[str]:
    """Fetch distinct scopes from agents table."""
    db = get_db()
    with db.read_connection() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT scope FROM agents ORDER BY scope")
        ).fetchall()
    return [r[0] for r in rows]


def _get_event_types() -> list[str]:
    """Fetch distinct event types."""
    db = get_db()
    with db.read_connection() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT type FROM events ORDER BY type")
        ).fetchall()
    return [r[0] for r in rows]


def _get_repo_name() -> str:
    """Fetch the first repository path for display in sidebar."""
    db = get_db()
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT path FROM repositories LIMIT 1")
        ).fetchone()
    if row is None:
        return "fleet"
    path = str(row[0])
    return path.rstrip("/").split("/")[-1]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def roster(request: Request, scope: str | None = None) -> HTMLResponse:
    """Agent Roster — main dashboard view."""
    templates = get_templates()
    try:
        agents = _query_agents(scope)
        scopes = _get_scopes()
        repo_name = _get_repo_name()
    except (SQLAlchemyError, RuntimeError) as exc:  # Maps SQLAlchemyError → 500
        return templates.TemplateResponse(
            request,
            "roster.html",
            {
                "error": str(exc),
                "agents": [],
                "scopes": [],
                "current_scope": scope,
                "repo_name": "fleet",
                "relative_time": _relative_time,
                "format_cost": _format_cost,
            },
        )
    return templates.TemplateResponse(
        request,
        "roster.html",
        {
            "agents": agents,
            "scopes": scopes,
            "current_scope": scope,
            "repo_name": repo_name,
            "relative_time": _relative_time,
            "format_cost": _format_cost,
        },
    )


@router.get("/agents/{agent_id}/conversation", response_class=HTMLResponse)
async def conversation(
    request: Request,
    agent_id: str,
) -> HTMLResponse:
    """Agent Conversation — event log for one agent with SSE live tail."""
    templates = get_templates()
    try:
        agent = _query_one_agent(agent_id)
        if agent is None:
            return templates.TemplateResponse(
                request,
                "conversation.html",
                {
                    "error": f"Agent {agent_id!r} not found",
                    "agent": None,
                    "events": [],
                    "last_event_id": 0,
                },
                status_code=404,
            )
        events = _query_events(scope=agent["scope"], agent_id=agent_id)
        last_event_id = events[0]["id"] if events else 0
        # Parse payload_json for each event
        for ev in events:
            try:
                ev["payload"] = json.loads(ev.get("payload_json") or "{}")
            except (ValueError, TypeError):
                ev["payload"] = {}
    except (SQLAlchemyError, RuntimeError) as exc:  # Maps SQLAlchemyError → 500
        return templates.TemplateResponse(
            request,
            "conversation.html",
            {
                "error": str(exc),
                "agent": None,
                "events": [],
                "last_event_id": 0,
            },
        )
    return templates.TemplateResponse(
        request,
        "conversation.html",
        {
            "agent": agent,
            "events": list(reversed(events)),  # oldest first for thread display
            "last_event_id": last_event_id,
            "relative_time": _relative_time,
        },
    )


@router.get("/timeline", response_class=HTMLResponse)
async def timeline(
    request: Request,
    scope: str | None = None,
    type_filter: str | None = None,
    agent_id: str | None = None,
) -> HTMLResponse:
    """Event Timeline — filterable event log across all agents."""
    templates = get_templates()
    try:
        events = _query_events(
            scope=scope,
            agent_id=agent_id,
            type_filter=type_filter,
        )
        for ev in events:
            try:
                ev["payload"] = json.loads(ev.get("payload_json") or "{}")
            except (ValueError, TypeError):
                ev["payload"] = {}
        agents = _query_agents(scope)
        event_types = _get_event_types()
        scopes = _get_scopes()
        repo_name = _get_repo_name()
    except (SQLAlchemyError, RuntimeError) as exc:  # Maps SQLAlchemyError → 500
        return templates.TemplateResponse(
            request,
            "timeline.html",
            {
                "error": str(exc),
                "events": [],
                "agents": [],
                "event_types": [],
                "scopes": [],
                "current_scope": scope,
                "current_type": type_filter,
                "current_agent": agent_id,
                "repo_name": "fleet",
            },
        )
    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "events": events,
            "agents": agents,
            "event_types": event_types,
            "scopes": scopes,
            "current_scope": scope,
            "current_type": type_filter,
            "current_agent": agent_id,
            "repo_name": repo_name,
        },
    )


@router.get("/worktrees/{worktree_id}", response_class=HTMLResponse)
async def worktree_view(request: Request, worktree_id: str) -> HTMLResponse:
    """Worktree / Diff view."""
    templates = get_templates()
    try:
        wt = _query_one_worktree(worktree_id)
        if wt is None:
            return templates.TemplateResponse(
                request,
                "worktree.html",
                {
                    "error": f"Worktree {worktree_id!r} not found",
                    "worktree": None,
                    "repo_name": "fleet",
                },
                status_code=404,
            )
        repo_name = _get_repo_name()
        try:
            wt["owned_paths"] = json.loads(wt.get("owned_paths_json") or "[]")
        except (ValueError, TypeError):
            wt["owned_paths"] = []
    except (SQLAlchemyError, RuntimeError) as exc:  # Maps SQLAlchemyError → 500
        return templates.TemplateResponse(
            request,
            "worktree.html",
            {
                "error": str(exc),
                "worktree": None,
                "repo_name": "fleet",
            },
        )
    return templates.TemplateResponse(
        request,
        "worktree.html",
        {
            "worktree": wt,
            "repo_name": repo_name,
        },
    )


@router.get("/tasks/{task_id}/validation", response_class=HTMLResponse)
async def validation_view(request: Request, task_id: str) -> HTMLResponse:
    """Validation & Merge view for a task."""
    templates = get_templates()
    try:
        task = _query_task_with_evidence(task_id)
        if task is None:
            return templates.TemplateResponse(
                request,
                "validation.html",
                {
                    "error": f"Task {task_id!r} not found",
                    "task": None,
                    "repo_name": "fleet",
                    "checklist_ok": False,
                },
                status_code=404,
            )
        repo_name = _get_repo_name()
        evidence = task.get("evidence", [])
        all_pass = bool(evidence) and all(
            e["status"] in ("pass", "skip") for e in evidence
        )
        any_fail = any(e["status"] == "fail" for e in evidence)
        checklist_ok = all_pass and not any_fail
    except (SQLAlchemyError, RuntimeError) as exc:  # Maps SQLAlchemyError → 500
        return templates.TemplateResponse(
            request,
            "validation.html",
            {
                "error": str(exc),
                "task": None,
                "repo_name": "fleet",
                "checklist_ok": False,
            },
        )
    return templates.TemplateResponse(
        request,
        "validation.html",
        {
            "task": task,
            "evidence": evidence,
            "checklist_ok": checklist_ok,
            "any_fail": any_fail,
            "repo_name": repo_name,
        },
    )


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_view(request: Request, scope: str | None = None) -> HTMLResponse:
    """Approval Queue view."""
    templates = get_templates()
    try:
        data = _query_approvals(scope)
        scopes = _get_scopes()
        repo_name = _get_repo_name()
    except (SQLAlchemyError, RuntimeError) as exc:  # Maps SQLAlchemyError → 500
        return templates.TemplateResponse(
            request,
            "approvals.html",
            {
                "error": str(exc),
                "pending": [],
                "decided": [],
                "scopes": [],
                "current_scope": scope,
                "repo_name": "fleet",
                "relative_time": _relative_time,
            },
        )
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "pending": data["pending"],
            "decided": data["decided"],
            "scopes": scopes,
            "current_scope": scope,
            "repo_name": repo_name,
            "relative_time": _relative_time,
        },
    )


@router.post("/approvals/{approval_id}/decide", response_class=HTMLResponse)
async def decide_approval(
    request: Request,
    approval_id: str,
    decision: str = Form(...),
    comment: str = Form(""),
) -> HTMLResponse:
    """Inline htmx endpoint for approving/denying an approval.

    Returns an HTML fragment (the updated approval row) for htmx to swap.
    """
    templates = get_templates()
    if decision not in ("approve", "deny"):
        return HTMLResponse(
            "<p class='error-text'>Invalid decision. Must be approve or deny.</p>",
            status_code=400,
        )

    if _approval_svc is None:
        return HTMLResponse(
            "<p class='error-text'>Approval service unavailable.</p>",
            status_code=503,
        )

    try:
        approval_record = await _approval_svc.decide(
            approval_id, decision, comment=comment  # type: ignore[arg-type]
        )
    except KeyError:
        return HTMLResponse(
            "<p class='error-text'>Approval not found.</p>",
            status_code=404,
        )
    except ValueError as exc:
        return HTMLResponse(
            f"<p class='error-text'>Decision failed: {exc}</p>",
            status_code=400,
        )
    except SQLAlchemyError as exc:  # DB write failure
        return HTMLResponse(
            f"<p class='error-text'>Decision failed: {exc}</p>",
            status_code=500,
        )

    approval = {
        "id": approval_record.id,
        "scope": approval_record.scope,
        "requester_agent_id": approval_record.requester_agent_id,
        "operation": approval_record.operation,
        "rationale": approval_record.rationale,
        "risk": approval_record.risk,
        "status": approval_record.status,
        "decided_by": approval_record.decided_by,
        "comment": approval_record.comment,
        "created_at": approval_record.created_at,
        "decided_at": approval_record.decided_at,
    }
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "pending": [],
            "decided": [approval],
            "scopes": [],
            "current_scope": None,
            "repo_name": "fleet",
            "relative_time": _relative_time,
            "htmx_fragment": "decided_row",
            "approval": approval,
        },
    )
