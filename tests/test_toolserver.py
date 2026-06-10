"""Tests for Task 4.1: MCP tool server + auth.

TDD: these tests are written BEFORE the implementation exists.
All tests should FAIL before fleet/toolserver/ and fleet/api/tools.py exist.

Test groups:
  1. Relay unit tests (mock httpx)
  2. API endpoint integration tests (FastAPI TestClient with injected mock services)
"""

from __future__ import annotations

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


def _build_tools_app(db: DatabaseManager, event_service: EventService) -> FastAPI:
    """Build a minimal FastAPI app wired with the tools router."""
    from fleet.api.tools import router, set_tool_services

    set_tool_services(
        agent_svc=None,   # injected per test via mock
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
    )

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
    from fleet.api.tools import router, set_tool_services

    mock_record = MagicMock()
    mock_record.id = "new-agent-id"
    mock_record.name = "worker-1"
    mock_record.scope = "scope-1"
    mock_record.status = "idle"

    mock_agent_svc = AsyncMock()
    mock_agent_svc.create_agent.return_value = mock_record

    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
    )
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
            "command": "pytest -q",
            "exit_code": 0,
            "summary": "All green",
        },
    )
    assert resp.status_code == 200

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT * FROM validation_evidence WHERE task_id = 'task-001'")
        ).fetchone()
    assert row is not None
    assert row.exit_code == 0
    assert row.command == "pytest -q"


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
