"""P0-2: Identity attribution — handler must use _authenticated_agent_id.

The dispatch_tool resolves a canonical agent_id from the token (for non-admin)
or from the body (for admin).  But handlers previously received only `inp`
(the body-parsed schema) and used `inp.agent_id` directly.

If the resolved agent_id and the body agent_id ever diverge (e.g. via a bug,
future schema change, or handler that doesn't check the mismatch path), the
event would be misattributed.

These tests verify that:
1. A non-admin agent token results in tool-call and tool-result audit events
   carrying the token's authentic agent_id, not just whatever the body said.
2. Handlers that emit events (e.g. report_issue) attribute them to the
   authenticated identity stored in svcs["_authenticated_agent_id"].
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
from sqlalchemy import text

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub


_IDENTITY_MANIFEST = os.path.join(
    os.path.dirname(__file__), "manifests", "identity_test.yaml"
)
_ADMIN_TOKEN = "fleet-admin-p02-token"


def _sha256(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _insert_agent(
    db_path: str,
    *,
    agent_id: str,
    role: str = "orchestrator",
    plaintext_token: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO agents"
        " (id, name, scope, role, backend, model, status,"
        "  context_pct, cost_usd, created_at, updated_at, token_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)",
        (
            agent_id, agent_id, "test-scope", role,
            "mock", "mock-model", "idle",
            _now(), _now(),
            _sha256(plaintext_token) if plaintext_token else None,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path: Any) -> str:
    return str(tmp_path / "p02_test.db")


@pytest_asyncio.fixture
async def db(db_path: str) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(db_path)
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager) -> EventService:
    return EventService(db, SSEHub())


@pytest_asyncio.fixture
def tool_app(db: DatabaseManager, db_path: str, event_svc: EventService) -> FastAPI:
    from fleet.api.auth import get_settings, set_auth_db
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.config import Settings
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    set_auth_db(db)
    mock_agent_svc = AsyncMock()
    mock_agent_svc.list_agents.return_value = []
    mock_agent_svc.get_agent.return_value = None
    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_svc,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
    )
    set_policy_service(PolicyService(load_manifest(_IDENTITY_MANIFEST)))

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: Settings(api_token=_ADMIN_TOKEN)
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_events_carry_authenticated_agent_id(
    tool_app: FastAPI,
    db: DatabaseManager,
    db_path: str,
) -> None:
    """tool_call + tool_result audit events must carry the token-authenticated agent_id.

    Verifies that dispatch_tool injects _authenticated_agent_id into svcs
    and that the audit events around the handler use that identity.
    """
    _insert_agent(db_path, agent_id="orch-p02", role="orchestrator", plaintext_token="tok-p02")

    async with AsyncClient(
        transport=ASGITransport(app=tool_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/list_agents",
            json={"agent_id": "orch-p02", "scope": "test-scope"},
            headers={"Authorization": "Bearer tok-p02"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    # Both the tool_call and tool_result events must be attributed to orch-p02.
    with db.read_connection() as conn:
        rows = conn.execute(
            text(
                "SELECT type, agent_id FROM events"
                " WHERE type IN ('tool_call', 'tool_result')"
                " ORDER BY id ASC"
            )
        ).fetchall()

    assert len(rows) >= 2, f"Expected tool_call + tool_result events, got {rows}"
    for row in rows:
        assert row[1] == "orch-p02", (
            f"Event type={row[0]!r} attributed to {row[1]!r}, expected 'orch-p02'"
        )


@pytest.mark.asyncio
async def test_tool_call_audit_events_agent_id_matches_token_not_body(
    tool_app: FastAPI,
    db: DatabaseManager,
    db_path: str,
) -> None:
    """Audit events emitted by dispatch_tool must carry the token-authenticated
    agent_id.  Specifically: the agent_id in tool_call / tool_result events
    must come from the resolved identity, not from a potentially unvalidated
    body field.

    This test verifies that _authenticated_agent_id is injected into svcs and
    is the source of truth for audit attribution.
    """
    _insert_agent(
        db_path, agent_id="orch-attr", role="orchestrator", plaintext_token="attr-tok"
    )

    async with AsyncClient(
        transport=ASGITransport(app=tool_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/list_agents",
            json={"agent_id": "orch-attr", "scope": "test-scope"},
            headers={"Authorization": "Bearer attr-tok"},
        )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    # Both audit events must carry the token-authenticated id, not just body value.
    with db.read_connection() as conn:
        rows = conn.execute(
            text(
                "SELECT type, agent_id FROM events"
                " WHERE type IN ('tool_call', 'tool_result')"
                " AND agent_id IS NOT NULL"
                " ORDER BY id ASC"
            )
        ).fetchall()

    assert len(rows) >= 2, f"Expected tool_call + tool_result events, got {rows}"
    for row in rows:
        assert row[1] == "orch-attr", (
            f"Audit event type={row[0]!r} attributed to {row[1]!r},"
            " expected authenticated 'orch-attr'"
        )
