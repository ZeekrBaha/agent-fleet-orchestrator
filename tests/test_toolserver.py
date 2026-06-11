"""Tests for Task 4.1: MCP tool server + auth.

TDD: these tests are written BEFORE the implementation exists.
All tests should FAIL before fleet/toolserver/ and fleet/api/tools.py exist.

Test groups:
  1. Relay unit tests (mock httpx)
  2. API endpoint integration tests (FastAPI TestClient with injected mock services)
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from fleet.api.auth import require_token
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PERMISSIVE_MANIFEST_PATH = os.path.join(
    os.path.dirname(__file__), "manifests", "permissive.yaml"
)

_DEFAULT_MANIFEST_PATH = os.path.join(
    os.path.dirname(__file__), "..", "fleet", "manifests", "default.yaml"
)


def _make_permissive_policy() -> Any:
    """Return a PolicyService loaded from the permissive test manifest."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    return PolicyService(load_manifest(_PERMISSIVE_MANIFEST_PATH))


def _make_test_role_agent_svc() -> Any:
    """Return a mock AgentService whose get_agent() always returns a test_role agent
    and whose list_agents() always returns an empty list (for spawn rate checks)."""
    mock_agent = MagicMock()
    mock_agent.role = "test_role"
    mock_agent.status = "idle"

    mock_agent_svc = AsyncMock()
    mock_agent_svc.get_agent.return_value = mock_agent
    mock_agent_svc.list_agents.return_value = []
    return mock_agent_svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _no_auth() -> None:
    """Dependency override that bypasses token auth."""
    return None


@pytest_asyncio.fixture()
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "tools_test.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture()
async def event_service(db: DatabaseManager) -> EventService:
    hub = SSEHub()
    return EventService(db, hub)


def _build_tools_app(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any = None,
) -> FastAPI:
    """Build a minimal FastAPI app wired with the tools router.

    Uses a permissive test manifest (test_role has all tools) so that
    non-policy tests can call any tool without worrying about ACL.
    """
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.review.evidence import EvidenceService

    _evidence_svc = evidence_svc if evidence_svc is not None else EvidenceService(db)

    set_tool_services(
        agent_svc=_make_test_role_agent_svc(),
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
        evidence_svc=_evidence_svc,
    )
    set_policy_service(_make_permissive_policy())

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_token] = _no_auth
    return app


@pytest_asyncio.fixture()
async def tools_client(
    db: DatabaseManager,
    event_service: EventService,
) -> AsyncIterator[AsyncClient]:
    app = _build_tools_app(db, event_service)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# 1. Relay unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_posts_to_correct_url() -> None:
    """FleetRelay.call() sends POST to /api/tools/{tool_name} with auth header."""
    from fleet.toolserver.relay import FleetRelay

    captured: dict[str, Any] = {}

    async def mock_post(url: str, **kwargs: Any) -> Response:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["json"] = kwargs.get("json", {})
        return Response(200, json={"result": "ok"})

    relay = FleetRelay(base_url="http://127.0.0.1:8000", token="test-token")
    with patch.object(relay._client, "post", side_effect=mock_post):
        await relay.call("spawn_worker", "agent-1", "scope-1", {"key": "val"})

    assert captured["url"] == "http://127.0.0.1:8000/api/tools/spawn_worker"
    assert captured["headers"].get("Authorization") == "Bearer test-token"


@pytest.mark.asyncio
async def test_relay_returns_response_json() -> None:
    """FleetRelay.call() returns the parsed JSON dict from the API response."""
    from fleet.toolserver.relay import FleetRelay

    expected = {"agent_id": "abc-123", "status": "idle"}

    async def mock_post(url: str, **kwargs: Any) -> Response:
        return Response(200, json=expected)

    relay = FleetRelay(base_url="http://127.0.0.1:8000", token="tok")
    with patch.object(relay._client, "post", side_effect=mock_post):
        result = await relay.call("list_agents", "agent-1", "scope-1", {})

    assert result == expected


@pytest.mark.asyncio
async def test_relay_raises_on_http_error() -> None:
    """FleetRelay.call() raises ToolCallError on non-2xx response."""
    from fleet.toolserver.relay import FleetRelay, ToolCallError

    async def mock_post(url: str, **kwargs: Any) -> Response:
        return Response(403, json={"detail": "Forbidden"})

    relay = FleetRelay(base_url="http://127.0.0.1:8000", token="bad-token")
    with patch.object(relay._client, "post", side_effect=mock_post):
        with pytest.raises(ToolCallError) as exc_info:
            await relay.call("list_agents", "agent-1", "scope-1", {})

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# 2. API endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_worker_calls_agent_service(
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """POST /api/tools/spawn_worker delegates to AgentService.create_agent()."""
    from fleet.api.tools import router, set_policy_service, set_tool_services

    mock_record = MagicMock()
    mock_record.id = "new-agent-id"
    mock_record.name = "worker-1"
    mock_record.scope = "scope-1"
    mock_record.status = "idle"

    # Calling agent is orchestrator so policy allows spawn_worker.
    calling_agent = MagicMock()
    calling_agent.role = "orchestrator"

    mock_agent_svc = AsyncMock()
    mock_agent_svc.create_agent.return_value = mock_record
    mock_agent_svc.get_agent.return_value = calling_agent
    mock_agent_svc.list_agents.return_value = []  # for spawn rate check

    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
    )
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    policy_svc = PolicyService(load_manifest(_DEFAULT_MANIFEST_PATH))
    set_policy_service(policy_svc)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_token] = _no_auth

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/spawn_worker",
            json={
                "agent_id": "orchestrator-1",
                "scope": "scope-1",
                "name": "worker-1",
                "role": "coder",
                "task_description": "Do work",
            },
        )

    assert resp.status_code == 200
    mock_agent_svc.create_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_validation_inserts_row(
    tools_client: AsyncClient,
    db: DatabaseManager,
) -> None:
    """POST /api/tools/record_validation inserts a row into validation_evidence."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    # validation_evidence requires a task_id FK — insert a task row first
    now = datetime.now(UTC).isoformat()

    def _insert_task(conn: Any) -> None:
        conn.execute(
            text(
                "INSERT INTO tasks (id, scope, title, description, status,"
                " created_at, updated_at)"
                " VALUES (:id, :scope, :title, :desc, :status, :now, :now)"
            ),
            {
                "id": "task-001",
                "scope": "scope-1",
                "title": "Test task",
                "desc": "desc",
                "status": "open",
                "now": now,
            },
        )
        conn.commit()

    await db.write(_insert_task)

    resp = await tools_client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": "agent-1",
            "scope": "scope-1",
            "task_id": "task-001",
            "check_name": "pytest -q",
            "status": "pass",
            "output": "All green",
        },
    )
    assert resp.status_code == 200

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT * FROM validation_evidence WHERE task_id = 'task-001'")
        ).fetchone()
    assert row is not None
    assert row.status == "pass"
    assert row.check_name == "pytest -q"


@pytest.mark.asyncio
async def test_request_approval_inserts_row(
    tools_client: AsyncClient,
    db: DatabaseManager,
) -> None:
    """POST /api/tools/request_approval inserts a row into the approvals table."""
    from sqlalchemy import text

    resp = await tools_client.post(
        "/api/tools/request_approval",
        json={
            "agent_id": "agent-1",
            "scope": "scope-1",
            "operation": "delete production data",
            "rationale": "Required for cleanup",
            "risk": "HIGH — cannot be undone",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    approval_id = data.get("id")
    assert approval_id

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT * FROM approvals WHERE id = :id"), {"id": approval_id}
        ).fetchone()
    assert row is not None
    assert row.status == "pending"
    assert row.operation == "delete production data"


@pytest.mark.asyncio
async def test_memory_write_inserts_row(
    tools_client: AsyncClient,
    db: DatabaseManager,
) -> None:
    """POST /api/tools/memory_write inserts a row into the memory table."""
    from sqlalchemy import text

    resp = await tools_client.post(
        "/api/tools/memory_write",
        json={
            "agent_id": "agent-1",
            "scope": "scope-1",
            "kind": "command_recipe",
            "title": "How to run tests",
            "body": "uv run pytest -q",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    mem_id = data.get("id")
    assert mem_id

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT * FROM memory WHERE id = :id"), {"id": mem_id}
        ).fetchone()
    assert row is not None
    assert row.kind == "command_recipe"
    assert row.title == "How to run tests"


@pytest.mark.asyncio
async def test_tool_call_emits_audit_events(
    tools_client: AsyncClient,
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """After any tool call, both tool_call and tool_result events are in the DB."""
    from sqlalchemy import text

    resp = await tools_client.post(
        "/api/tools/update_progress",
        json={
            "agent_id": "agent-1",
            "scope": "audit-scope",
            "message": "50% complete",
            "percent": 50,
        },
    )
    assert resp.status_code == 200

    with db.read_connection() as conn:
        rows = conn.execute(
            text(
                "SELECT type FROM events WHERE scope = 'audit-scope'"
                " ORDER BY id ASC"
            )
        ).fetchall()

    event_types = [r.type for r in rows]
    assert "tool_call" in event_types
    assert "tool_result" in event_types


@pytest.mark.asyncio
async def test_invalid_input_returns_422(tools_client: AsyncClient) -> None:
    """POST with wrong types (exit_code='not-an-int') returns 422 validation error."""
    resp = await tools_client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": "agent-1",
            "scope": "scope-1",
            "task_id": "task-001",
            "command": "pytest",
            "exit_code": "not-an-int",  # wrong type
            "summary": "summary",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unknown_tool_returns_404(tools_client: AsyncClient) -> None:
    """POST /api/tools/nonexistent_tool returns 404."""
    resp = await tools_client.post(
        "/api/tools/nonexistent_tool",
        json={"agent_id": "agent-1", "scope": "scope-1"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tool_error_emits_tool_result_error_event(
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """When a tool handler raises, a tool_result_error event is emitted."""
    from sqlalchemy import text

    from fleet.api.tools import router, set_policy_service, set_tool_services

    # Inject a worktree_svc that raises ValueError to trigger the error path.
    mock_worktree_svc = AsyncMock()
    mock_worktree_svc.get_wip_report.side_effect = ValueError("worktree gone")

    # get_agent("agent-1") → calling agent with coder role (has worker_wip).
    # get_agent("agent-99") → target agent with worktree_id set.
    mock_calling_agent = MagicMock()
    mock_calling_agent.role = "coder"
    mock_target_agent = MagicMock()
    mock_target_agent.worktree_id = "wt-99"

    async def _get_agent(agent_id: str) -> Any:
        if agent_id == "agent-1":
            return mock_calling_agent
        return mock_target_agent

    mock_agent_svc = AsyncMock()
    mock_agent_svc.get_agent.side_effect = _get_agent

    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=mock_worktree_svc,
        db=db,
    )
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    policy_svc = PolicyService(load_manifest(_DEFAULT_MANIFEST_PATH))
    set_policy_service(policy_svc)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_token] = _no_auth

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/worker_wip",
            json={
                "agent_id": "agent-1",
                "scope": "error-scope",
                "target_agent_id": "agent-99",
            },
        )

    # Handler raises ValueError → 400; the important thing is the audit event.
    assert resp.status_code == 400

    with db.read_connection() as conn:
        rows = conn.execute(
            text(
                "SELECT type FROM events WHERE scope = 'error-scope'"
                " ORDER BY id ASC"
            )
        ).fetchall()

    event_types = [r.type for r in rows]
    assert "tool_call" in event_types
    assert "tool_result_error" in event_types
    assert "tool_result" not in event_types


@pytest.mark.asyncio
async def test_check_conflict_returns_has_conflict(
    db: DatabaseManager,
    event_service: EventService,
    tmp_path: Any,
) -> None:
    """POST /api/tools/check_conflict returns has_conflict for a diverged branch."""
    from datetime import UTC, datetime

    from sqlalchemy import text

    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService
    from tests.fixtures.gitrepo import GitRepoFactory

    # Create a repo with conflict potential (main and feature both modify file.txt).
    factory = GitRepoFactory(tmp_path)
    repo_path = factory.make_with_conflict_potential(name="conflict-repo")

    now = datetime.now(UTC).isoformat()
    repo_id = "repo-conflict-1"
    agent_id = "agent-conflict-1"
    worktree_id = "wt-conflict-1"

    def _seed(conn: Any) -> None:
        conn.execute(
            text(
                "INSERT INTO repositories"
                " (id, path, default_branch, merge_policy_json, created_at)"
                " VALUES (:id, :path, 'main', '{}', :now)"
            ),
            {"id": repo_id, "path": str(repo_path), "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  created_at, updated_at)"
                " VALUES (:id, :name, 'scope-1', 'coder', 'mock',"
                "  'claude-sonnet-4-6', 'idle', :now, :now)"
            ),
            {"id": agent_id, "name": "conflict-agent", "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO worktrees"
                " (id, agent_id, repository_id, path, branch, base_branch,"
                "  owned_paths_json, status, created_at)"
                " VALUES (:id, :agent_id, :repo_id, :path, 'feature', 'main',"
                "  '[]', 'active', :now)"
            ),
            {
                "id": worktree_id,
                "agent_id": agent_id,
                "repo_id": repo_id,
                "path": str(repo_path),
                "now": now,
            },
        )
        conn.commit()

    await db.write(_seed)

    # Use a real AgentService so get_agent(agent_id) returns the seeded coder agent.
    inbox_svc = InboxService(db)
    agent_svc = AgentService(db, event_service, inbox_svc)

    set_tool_services(
        agent_svc=agent_svc,
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
    )
    # coder has check_conflict in its allowed_tools
    policy_svc = PolicyService(load_manifest(_DEFAULT_MANIFEST_PATH))
    set_policy_service(policy_svc)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_token] = _no_auth

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/check_conflict",
            json={
                "agent_id": agent_id,
                "scope": "scope-1",
                "worktree_id": worktree_id,
                "target_branch": "main",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "has_conflict" in data
    assert isinstance(data["has_conflict"], bool)
    # The conflict-potential repo always has a conflict between feature and main.
    assert data["has_conflict"] is True

    factory.cleanup()
