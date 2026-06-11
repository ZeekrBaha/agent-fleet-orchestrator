"""Fleet application entry point.

Start the server:
    uv run uvicorn fleet.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import fleet.api.agents as _agents_api
import fleet.api.approvals as _approvals_api
import fleet.api.merge as _merge_api
import fleet.api.review as _review_api
import fleet.api.tools as _tools_api
from fleet.agents.inbox import InboxService
from fleet.agents.service import AgentService
from fleet.api.agents import router as agents_router
from fleet.api.approvals import router as approvals_router
from fleet.api.events import router as events_router
from fleet.api.merge import router as merge_router
from fleet.api.review import router as review_router
from fleet.api.tools import router as tools_router
from fleet.api.workspaces import router as workspaces_router
from fleet.api.workspaces import set_workspace_service, set_worktree_service
from fleet.approvals.service import ApprovalService
from fleet.config import Settings
from fleet.dashboard.router import router as dashboard_router
from fleet.dashboard.router import set_approval_service as set_dash_approval_svc
from fleet.dashboard.router import set_db, set_templates
from fleet.db import DatabaseManager, init_db
from fleet.events.service import create_event_service
from fleet.events.sse import SSEHub
from fleet.policy.rules import load_default_manifest
from fleet.policy.service import PolicyService
from fleet.review.conflict import ConflictChecker
from fleet.review.evidence import EvidenceService
from fleet.review.lock import MergeLock
from fleet.review.merge import MergeService
from fleet.workspace.service import WorkspaceService
from fleet.workspace.worktree_service import WorktreeService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down lifecycle for Fleet."""
    settings = Settings()
    settings.validate_for_startup()

    manager: DatabaseManager = await init_db(settings.db_path)
    sse_hub = SSEHub()
    event_svc = create_event_service(manager, sse_hub)

    inbox_svc = InboxService(manager)
    approval_svc = ApprovalService(manager, event_svc)
    agent_svc = AgentService(
        manager,
        event_svc,
        inbox_svc,
        approval_svc=approval_svc,
    )
    workspace_svc = WorkspaceService(manager, event_svc)
    worktree_svc = WorktreeService(manager, event_svc, workspace_svc)
    evidence_svc = EvidenceService(manager)
    conflict_checker = ConflictChecker()
    merge_lock = MergeLock()
    merge_svc = MergeService(
        db=manager,
        event_service=event_svc,
        evidence_service=evidence_svc,
        conflict_checker=conflict_checker,
        lock=merge_lock,
    )
    manifest = load_default_manifest()
    policy_svc = PolicyService(manifest)

    fleet_pkg = pathlib.Path(__file__).parent
    templates_dir = fleet_pkg / "templates"
    static_dir = fleet_pkg / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    # Expose api_token to all templates so app.js can pass it to EventSource.
    templates.env.globals["api_token"] = settings.api_token

    # Wire dependency injection for module-level set_ functions.
    set_db(manager)
    set_templates(templates)
    set_dash_approval_svc(approval_svc)
    _agents_api.set_agent_service(agent_svc)
    _approvals_api.set_approval_service(approval_svc)
    _merge_api.set_merge_service(merge_svc)
    _review_api.set_evidence_service(evidence_svc)
    _tools_api.set_tool_services(
        agent_svc=agent_svc,
        event_svc=event_svc,
        workspace_svc=workspace_svc,
        worktree_svc=worktree_svc,
        db=manager,
        evidence_svc=evidence_svc,
        merge_svc=merge_svc,
    )
    _tools_api.set_policy_service(policy_svc)
    set_workspace_service(workspace_svc)
    set_worktree_service(worktree_svc)

    await agent_svc.restore_sessions()

    # Events router reads from app.state at request time (no set_ function).
    app.state.event_service = event_svc
    app.state.sse_hub = sse_hub

    if static_dir.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(static_dir)),
            name="static",
        )

    yield

    await agent_svc.stop_all()
    await manager.close()


app = FastAPI(title="Fleet", lifespan=lifespan)
app.include_router(agents_router)
app.include_router(events_router)
app.include_router(tools_router)
app.include_router(approvals_router)
app.include_router(merge_router)
app.include_router(review_router)
app.include_router(workspaces_router)
app.include_router(dashboard_router)
