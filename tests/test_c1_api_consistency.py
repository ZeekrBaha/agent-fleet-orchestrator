"""C1: API consistency fixes.

Behaviors tested:
1. GET /api/events?limit=9999 returns 422 (Query le=1000 validation).
2. execute_merge raising MergeInProgressError returns 409, not 500.
3. POST /api/review/tasks returns 201, not 200.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fleet.api.auth import require_token
from fleet.db import DatabaseManager, init_db
from fleet.events.service import create_event_service
from fleet.events.sse import SSEHub


def _no_auth() -> None:
    """Auth bypass for tests."""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path) -> DatabaseManager:
    manager = await init_db(str(tmp_path / "c1.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager):
    return create_event_service(db, SSEHub())


# ---------------------------------------------------------------------------
# 1. /api/events?limit= is clamped at 1000
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_limit_clamped_at_1000(db: DatabaseManager, event_svc) -> None:
    """GET /api/events?limit=9999 must return 422.

    Fails before C1: limit has no upper bound annotation.
    """
    from fleet.api.events import router as events_router

    app = FastAPI()
    app.state.event_service = event_svc
    app.state.sse_hub = SSEHub()
    app.dependency_overrides[require_token] = _no_auth
    app.include_router(events_router)

    # TestClient is fine here: 422 is returned before any async DB work.
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/events?scope=test&limit=9999")
    assert resp.status_code == 422, (
        f"Expected 422 for limit=9999 > 1000, got {resp.status_code}:\n{resp.text}"
    )


# ---------------------------------------------------------------------------
# 2. MergeInProgressError → 409
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_in_progress_returns_409(
    db: DatabaseManager, event_svc
) -> None:
    """execute_merge when merge already running must return 409, not 500.

    Fails before C1: MergeInProgressError falls through to generic Exception
    handler in tools.py → 500.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx
    from httpx import ASGITransport

    from fleet.api.auth import AgentIdentity, require_agent_identity
    from fleet.api.tool_schemas import ExecuteMergeInput
    from fleet.api.tools import (
        _TOOL_REGISTRY,
        set_policy_service,
        set_tool_services,
    )
    from fleet.api.tools import (
        router as tools_router,
    )
    from fleet.policy.rules import load_default_manifest
    from fleet.policy.service import PolicyService
    from fleet.review.lock import MergeInProgressError

    agent_svc_mock = AsyncMock()
    agent_svc_mock.get_agent = AsyncMock(return_value=None)
    set_tool_services(agent_svc_mock, event_svc, MagicMock(), MagicMock(), db)
    policy_svc = PolicyService(load_default_manifest())
    set_policy_service(policy_svc)

    app = FastAPI()
    app.state.event_service = event_svc
    app.dependency_overrides[require_agent_identity] = lambda: AgentIdentity(
        agent_id="agent-c1", role="orchestrator", is_admin=False
    )
    app.include_router(tools_router)

    mock_handler = AsyncMock(side_effect=MergeInProgressError("scope-c1"))
    patched = {"execute_merge": (ExecuteMergeInput, mock_handler)}
    with patch.dict(_TOOL_REGISTRY, patched):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/tools/execute_merge",
                json={
                    "agent_id": "agent-c1",
                    "scope": "scope-c1",
                    "worktree_id": "wt-1",
                },
            )

    assert resp.status_code == 409, (
        f"Expected 409 for MergeInProgressError, got {resp.status_code}:\n{resp.text}"
    )


# ---------------------------------------------------------------------------
# 3. POST /api/review/tasks returns 201
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_returns_201(db: DatabaseManager, event_svc) -> None:
    """POST /api/review/tasks must return 201 Created, not 200 OK.

    Fails before C1: endpoint uses default status_code=200.
    Uses AsyncClient+ASGITransport to avoid cross-event-loop deadlock
    between TestClient's sync loop and the DB writer queue.
    """
    import httpx
    from httpx import ASGITransport

    from fleet.api.review import router as review_router
    from fleet.api.review import set_evidence_service
    from fleet.review.evidence import EvidenceService

    set_evidence_service(EvidenceService(db))

    app = FastAPI()
    app.dependency_overrides[require_token] = _no_auth
    app.include_router(review_router)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/review/tasks",
            json={
                "scope": "scope-c1",
                "title": "test task",
                "description": "desc",
                "owner_agent_id": "agent-1",
            },
        )

    assert resp.status_code == 201, (
        f"Expected 201 Created, got {resp.status_code}:\n{resp.text}"
    )
