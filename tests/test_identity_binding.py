"""A1: Per-agent identity binding — TDD RED phase.

These tests must fail before the A1 implementation because:
  - require_agent_identity does not exist in fleet.api.auth
  - dispatch_tool still uses require_token (not per-agent auth)
  - agents.token_hash column does not exist (migration 0002 pending)

Tests verify:
  1. Token for agent-A + body claiming agent-B → 403 (mismatch)
  2. Worker token calling orchestrator-only tool → 403 (policy on auth role)
  3. Valid token with matching identity → 200
  4. Archived agent's token → 401
  5. Token absent → 401
  6. Admin token + body agent_id → 200 + emits admin_impersonation event
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub

_IDENTITY_MANIFEST = os.path.join(
    os.path.dirname(__file__), "manifests", "identity_test.yaml"
)
_ADMIN_TOKEN = "fleet-admin-test-token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _insert_agent(
    db_path: str,
    *,
    agent_id: str,
    role: str = "worker",
    status: str = "idle",
    plaintext_token: str | None = None,
) -> None:
    """Insert a minimal agent row directly (bypasses AgentService session start)."""
    token_hash = _sha256(plaintext_token) if plaintext_token else None
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO agents"
        " (id, name, scope, role, backend, model, status,"
        "  context_pct, cost_usd, created_at, updated_at, token_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)",
        (
            agent_id, agent_id, "test-scope", role, "mock", "mock-model",
            status, _now(), _now(), token_hash,
        ),
    )
    raw.commit()
    raw.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Any) -> str:
    return str(tmp_path / "identity_test.db")


@pytest_asyncio.fixture
async def db(db_path: str) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(db_path)
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager) -> EventService:
    hub = SSEHub()
    return EventService(db, hub)


@pytest_asyncio.fixture
def mock_agent_svc() -> Any:
    svc = AsyncMock()
    svc.list_agents.return_value = []
    svc.get_agent.return_value = None
    return svc


@pytest_asyncio.fixture
def identity_app(
    db: DatabaseManager,
    db_path: str,
    event_svc: EventService,
    mock_agent_svc: Any,
) -> FastAPI:
    """Build tools app wired with real require_agent_identity (not overridden)."""
    from fleet.api.auth import set_auth_db  # RED: ImportError if A1 not impl
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.config import Settings
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    set_auth_db(db)

    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_svc,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
    )
    policy_svc = PolicyService(load_manifest(_IDENTITY_MANIFEST))
    set_policy_service(policy_svc)

    from fleet.api.auth import get_settings
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: Settings(
        api_token=_ADMIN_TOKEN
    )
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_for_agent_a_claiming_agent_b_returns_403(
    identity_app: FastAPI,
    db_path: str,
) -> None:
    """Agent-A's token with body claiming agent-B → 403 identity mismatch."""
    _insert_agent(
        db_path, agent_id="agent-a", role="orchestrator", plaintext_token="token-a"
    )
    _insert_agent(db_path, agent_id="agent-b", role="orchestrator")

    async with AsyncClient(
        transport=ASGITransport(app=identity_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/list_agents",
            json={"agent_id": "agent-b", "scope": "test-scope"},
            headers={"Authorization": "Bearer token-a"},
        )

    assert resp.status_code == 403, (
        f"Expected 403 identity mismatch, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_worker_token_on_orchestrator_tool_returns_403(
    identity_app: FastAPI,
    db_path: str,
) -> None:
    """Worker's per-agent token calling orchestrator-only tool → 403.

    The policy check uses the AUTHENTICATED role (worker) not the body's
    agent_id.  stop_agent is only allowed for orchestrators in identity_test.yaml.
    """
    _insert_agent(
        db_path, agent_id="worker-1", role="worker", plaintext_token="w-token"
    )

    async with AsyncClient(
        transport=ASGITransport(app=identity_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/stop_agent",
            json={
                "agent_id": "worker-1",
                "scope": "test-scope",
                "target_agent_id": "some-other",
                "reason": "test",
            },
            headers={"Authorization": "Bearer w-token"},
        )

    assert resp.status_code == 403, (
        f"Expected 403 policy denied, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_valid_token_matching_identity_returns_200(
    identity_app: FastAPI,
    db_path: str,
) -> None:
    """Valid per-agent token with matching body agent_id and allowed tool → 200."""
    _insert_agent(
        db_path, agent_id="orch-1", role="orchestrator", plaintext_token="orch-tok"
    )

    async with AsyncClient(
        transport=ASGITransport(app=identity_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/list_agents",
            json={"agent_id": "orch-1", "scope": "test-scope"},
            headers={"Authorization": "Bearer orch-tok"},
        )

    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_archived_agent_token_returns_401(
    identity_app: FastAPI,
    db_path: str,
) -> None:
    """Token for an archived agent → 401 (revoked)."""
    _insert_agent(
        db_path,
        agent_id="retired-1",
        role="worker",
        status="archived",
        plaintext_token="old-token",
    )

    async with AsyncClient(
        transport=ASGITransport(app=identity_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/list_agents",
            json={"agent_id": "retired-1", "scope": "test-scope"},
            headers={"Authorization": "Bearer old-token"},
        )

    assert resp.status_code == 401, (
        f"Expected 401 (archived = revoked), got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_absent_token_returns_401(
    identity_app: FastAPI,
    db_path: str,
) -> None:
    """No Authorization header on tool dispatch → 401."""
    _insert_agent(
        db_path, agent_id="orch-2", role="orchestrator", plaintext_token="tok-2"
    )

    async with AsyncClient(
        transport=ASGITransport(app=identity_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/list_agents",
            json={"agent_id": "orch-2", "scope": "test-scope"},
            # No Authorization header
        )

    assert resp.status_code == 401, (
        f"Expected 401 (no token), got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_admin_token_impersonation_emits_event(
    identity_app: FastAPI,
    db: DatabaseManager,
    db_path: str,
) -> None:
    """Admin token + body agent_id → 200 and admin_impersonation event in DB."""
    _insert_agent(db_path, agent_id="some-agent", role="orchestrator")

    async with AsyncClient(
        transport=ASGITransport(app=identity_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/list_agents",
            json={"agent_id": "some-agent", "scope": "test-scope"},
            headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"},
        )

    assert resp.status_code == 200, (
        f"Expected 200 for admin impersonation, got {resp.status_code}: {resp.text}"
    )

    # Verify admin_impersonation event was emitted
    with db.read_connection() as conn:
        from sqlalchemy import text
        row = conn.execute(
            text("SELECT COUNT(*) FROM events WHERE type='admin_impersonation'")
        ).scalar()
    assert row and row >= 1, (
        "Expected admin_impersonation event in events table after admin impersonation"
    )
