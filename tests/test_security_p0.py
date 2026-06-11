"""P0 security regression tests.

Written TDD: these tests must FAIL before the fixes are applied.

P0-1: Dashboard router unauthenticated
P0-2: Role ACL spoofable via caller-supplied inp.role
P0-3: GET /api/approvals missing auth
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PERMISSIVE_MANIFEST_PATH = os.path.join(
    os.path.dirname(__file__), "manifests", "permissive.yaml"
)

_SECURITY_P0_MANIFEST_PATH = os.path.join(
    os.path.dirname(__file__), "manifests", "security_p0.yaml"
)


def _no_auth() -> None:
    """Dependency override that bypasses token auth."""
    return None


def _make_worker_calling_agent_svc() -> Any:
    """Return a mock AgentService whose get_agent() returns a *worker* agent."""
    mock_agent = MagicMock()
    mock_agent.role = "worker"
    mock_agent.status = "idle"

    mock_agent_svc = AsyncMock()
    mock_agent_svc.get_agent.return_value = mock_agent
    mock_agent_svc.list_agents.return_value = []
    return mock_agent_svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "security_p0_test.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_service(db: DatabaseManager) -> EventService:
    hub = SSEHub()
    return EventService(db, hub)


# ---------------------------------------------------------------------------
# P0-3: GET /api/approvals must require auth
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def approvals_app_no_auth_override(
    db: DatabaseManager,
    event_service: EventService,
) -> FastAPI:
    """Build approvals app WITHOUT overriding require_token."""
    from fleet.api.approvals import router, set_approval_service
    from fleet.approvals.service import ApprovalService

    approval_svc = ApprovalService(db=db, event_service=event_service)
    await approval_svc.load_pending()
    set_approval_service(approval_svc)

    app = FastAPI()
    app.include_router(router)
    # Intentionally NOT overriding require_token — auth must be enforced.
    return app


@pytest.mark.asyncio
async def test_get_approvals_requires_auth(
    approvals_app_no_auth_override: FastAPI,
) -> None:
    """GET /api/approvals must return 401 when no auth token is provided.

    RED: this test fails before the fix because list_pending_approvals has no
    require_token dependency.
    """
    async with AsyncClient(
        transport=ASGITransport(app=approvals_app_no_auth_override),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/approvals?scope=any")

    assert response.status_code == 401, (
        f"Expected 401 Unauthorized but got {response.status_code}. "
        "GET /api/approvals is missing require_token dependency."
    )


# ---------------------------------------------------------------------------
# P0-1: Dashboard routes must require auth
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def dashboard_app_no_auth_override(
    db: DatabaseManager,
    event_service: EventService,
    tmp_path: Any,
) -> FastAPI:
    """Build dashboard app WITHOUT overriding require_token."""
    import pathlib

    from fastapi.templating import Jinja2Templates

    from fleet.approvals.service import ApprovalService
    from fleet.dashboard.router import (
        router,
        set_approval_service,
        set_db,
        set_templates,
    )

    # Create a minimal roster.html template so the roster route can render
    # (auth is checked before rendering, but we need a valid templates dir).
    tmpl_dir = tmp_path / "templates"
    tmpl_dir.mkdir()
    (tmpl_dir / "roster.html").write_text("")
    (tmpl_dir / "approvals.html").write_text("")

    templates = Jinja2Templates(directory=str(tmpl_dir))
    approval_svc = ApprovalService(db=db, event_service=event_service)
    await approval_svc.load_pending()

    set_db(db)
    set_templates(templates)
    set_approval_service(approval_svc)

    app = FastAPI()
    app.include_router(router)
    # Intentionally NOT overriding require_token.
    return app


@pytest.mark.asyncio
async def test_dashboard_decide_requires_auth(
    dashboard_app_no_auth_override: FastAPI,
) -> None:
    """POST /dashboard/approvals/{id}/decide must return 401 without auth.

    RED: this test fails before the fix because the dashboard router has no
    require_token dependency.
    """
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app_no_auth_override),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/dashboard/approvals/fake-id/decide",
            data={"decision": "approve", "comment": ""},
        )

    assert response.status_code == 401, (
        f"Expected 401 Unauthorized but got {response.status_code}. "
        "POST /dashboard/approvals/{{id}}/decide is unauthenticated."
    )


@pytest.mark.asyncio
async def test_dashboard_roster_requires_auth(
    dashboard_app_no_auth_override: FastAPI,
) -> None:
    """GET /dashboard/ must return 401 without auth.

    RED: fails before fix — the roster (agent list) endpoint leaks data without auth.
    """
    async with AsyncClient(
        transport=ASGITransport(app=dashboard_app_no_auth_override),
        base_url="http://test",
    ) as client:
        response = await client.get("/dashboard/")

    assert response.status_code == 401, (
        f"Expected 401 Unauthorized but got {response.status_code}. "
        "GET /dashboard/ is unauthenticated."
    )


# ---------------------------------------------------------------------------
# P0-2: Role escalation via spawn_worker must be denied
# ---------------------------------------------------------------------------


def _build_tools_app_with_worker_caller(
    db: DatabaseManager,
    event_service: EventService,
) -> FastAPI:
    """Build a tools app where the calling agent has role='worker'.

    Uses security_p0.yaml which grants 'worker' access to spawn_worker so that
    the policy layer passes and the new role-ACL check in _handle_spawn_worker
    is what determines the outcome.
    """
    from fleet.api.auth import require_token
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    set_tool_services(
        agent_svc=_make_worker_calling_agent_svc(),
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
    )
    policy_svc = PolicyService(load_manifest(_SECURITY_P0_MANIFEST_PATH))
    set_policy_service(policy_svc)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_token] = _no_auth
    return app


@pytest.mark.asyncio
async def test_worker_cannot_spawn_orchestrator(
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """A worker calling spawn_worker with role='orchestrator' must get 403.

    RED: this test fails before the fix because _handle_spawn_worker passes
    inp.role directly to create_agent without an allowlist check.
    """
    app = _build_tools_app_with_worker_caller(db, event_service)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/tools/spawn_worker",
            json={
                "agent_id": "worker-agent-1",
                "scope": "scope-1",
                "name": "evil-orchestrator",
                "role": "orchestrator",
                "task_description": "Trying to escalate",
            },
        )

    assert response.status_code == 403, (
        f"Expected 403 but got {response.status_code}. "
        "Worker must not be able to spawn an orchestrator (role escalation)."
    )


@pytest.mark.asyncio
async def test_worker_cannot_spawn_reviewer(
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """A worker calling spawn_worker with role='reviewer' must get 403.

    RED: reviewer is also a privileged role — workers cannot spawn it.
    """
    app = _build_tools_app_with_worker_caller(db, event_service)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/tools/spawn_worker",
            json={
                "agent_id": "worker-agent-1",
                "scope": "scope-1",
                "name": "evil-reviewer",
                "role": "reviewer",
                "task_description": "Trying to escalate",
            },
        )

    assert response.status_code == 403, (
        f"Expected 403 but got {response.status_code}. "
        "Worker must not be able to spawn a reviewer (role escalation)."
    )


@pytest.mark.asyncio
async def test_worker_can_spawn_worker(
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """A worker calling spawn_worker with role='worker' must succeed (200).

    GREEN after fix: workers are allowed to spawn other workers.
    """
    from unittest.mock import MagicMock

    # Override agent_svc so create_agent returns a valid record
    mock_record = MagicMock()
    mock_record.id = "new-worker-id"
    mock_record.name = "sub-worker"
    mock_record.scope = "scope-1"
    mock_record.status = "idle"

    calling_agent = MagicMock()
    calling_agent.role = "worker"
    calling_agent.status = "idle"

    mock_agent_svc = AsyncMock()
    mock_agent_svc.create_agent.return_value = mock_record
    mock_agent_svc.get_agent.return_value = calling_agent
    mock_agent_svc.list_agents.return_value = []

    from fleet.api.auth import require_token
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
    )
    policy_svc = PolicyService(load_manifest(_SECURITY_P0_MANIFEST_PATH))
    set_policy_service(policy_svc)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_token] = _no_auth

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/tools/spawn_worker",
            json={
                "agent_id": "worker-agent-1",
                "scope": "scope-1",
                "name": "sub-worker",
                "role": "worker",
                "task_description": "Do some sub-work",
            },
        )

    assert response.status_code == 200, (
        f"Expected 200 but got {response.status_code}. "
        "Workers must be allowed to spawn other workers."
    )


@pytest.mark.asyncio
async def test_orchestrator_can_spawn_orchestrator(
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """An orchestrator calling spawn_worker with role='orchestrator' must succeed (200).

    Orchestrators are unrestricted in _SPAWN_ROLE_ALLOWLIST (None entry),
    so they may spawn any role including another orchestrator.
    """
    from unittest.mock import MagicMock

    mock_record = MagicMock()
    mock_record.id = "new-orchestrator-id"
    mock_record.name = "sub-orchestrator"
    mock_record.scope = "scope-1"
    mock_record.status = "idle"

    calling_agent = MagicMock()
    calling_agent.role = "orchestrator"
    calling_agent.status = "idle"

    mock_agent_svc = AsyncMock()
    mock_agent_svc.create_agent.return_value = mock_record
    mock_agent_svc.get_agent.return_value = calling_agent
    mock_agent_svc.list_agents.return_value = []

    from fleet.api.auth import require_token
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
    )
    policy_svc = PolicyService(load_manifest(_SECURITY_P0_MANIFEST_PATH))
    set_policy_service(policy_svc)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_token] = _no_auth

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/tools/spawn_worker",
            json={
                "agent_id": "orchestrator-agent-1",
                "scope": "scope-1",
                "name": "sub-orchestrator",
                "role": "orchestrator",
                "task_description": "Coordinate sub-tasks",
            },
        )

    assert response.status_code == 200, (
        f"Expected 200 but got {response.status_code}. "
        "Orchestrators must be allowed to spawn other orchestrators (unrestricted allowlist)."
    )
