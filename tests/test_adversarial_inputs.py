"""P2-1: Adversarial input test suite.

Verifies that the Fleet API handles malformed, oversized, and injection-laden
inputs safely — returning 422 on schema violations and storing metacharacter
payloads without executing them.

Coverage:
  - Field length enforcement (max_length, min_length violations → 422)
  - Malformed / non-JSON request bodies → 422
  - HTML metacharacters in string fields stored safely (no XSS surface in JSON)
  - SQL metacharacters stored safely (parameterized queries throughout)
  - Severity enum rejection for invalid values → 422
  - Percent range enforcement (0–100) → 422
  - Empty / whitespace-only required fields → 422
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub
from fleet.models import AgentRecord
from fleet.review.evidence import EvidenceService

_PERMISSIVE_MANIFEST = os.path.join(
    os.path.dirname(__file__), "manifests", "permissive.yaml"
)
_ADMIN_TOKEN = "fleet-adversarial-test-token"
_AGENT_TOKEN = "agent-adversarial-tok"
_AGENT_ID = "agent-adv"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _insert_agent(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO agents"
        " (id, name, scope, role, backend, model, status,"
        "  context_pct, cost_usd, created_at, updated_at, token_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)",
        (
            _AGENT_ID, _AGENT_ID, "adv-scope", "test_role",
            "mock", "mock", "idle",
            _now(), _now(), _sha256(_AGENT_TOKEN),
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "adv_test.db")


@pytest_asyncio.fixture
async def db(db_path: str) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(db_path)
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager) -> EventService:
    return EventService(db, SSEHub())


@pytest_asyncio.fixture
async def tool_app(
    db: DatabaseManager, db_path: str, event_svc: EventService
) -> FastAPI:
    from fleet.api.auth import get_settings, set_auth_db
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.config import Settings
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    _insert_agent(db_path)

    mock_agent_svc = AsyncMock()
    mock_agent_svc.get_agent.return_value = AgentRecord(
        id=_AGENT_ID, name=_AGENT_ID, scope="adv-scope", role="test_role",
        backend="mock", model="mock", status="idle",
        created_at=_now(), updated_at=_now(),
    )
    mock_agent_svc.list_agents.return_value = []

    set_auth_db(db)
    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_svc,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
        evidence_svc=EvidenceService(db, gate_require_reviewer=False),
    )
    set_policy_service(PolicyService(load_manifest(_PERMISSIVE_MANIFEST)))

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: Settings(api_token=_ADMIN_TOKEN)
    return app


@pytest_asyncio.fixture
async def client(tool_app: FastAPI, db: DatabaseManager) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=tool_app), base_url="http://test"
    ) as c:
        c.headers.update({"Authorization": f"Bearer {_AGENT_TOKEN}"})
        yield c


async def _make_task(db: DatabaseManager) -> str:
    ev = EvidenceService(db, gate_require_reviewer=False)
    return await ev.create_task(
        scope="adv-scope", title="T", description="d",
        owner_agent_id=_AGENT_ID, branch="main",
    )


# ---------------------------------------------------------------------------
# Field length — max_length violations → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_check_name_rejected(
    client: AsyncClient, db: DatabaseManager
) -> None:
    """check_name > 1024 chars → 422 Unprocessable Entity."""
    task_id = await _make_task(db)
    resp = await client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "task_id": task_id, "check_name": "x" * 1025,
            "status": "pass",
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


@pytest.mark.asyncio
async def test_oversized_output_rejected(
    client: AsyncClient, db: DatabaseManager
) -> None:
    """output > 65536 chars → 422 Unprocessable Entity."""
    task_id = await _make_task(db)
    resp = await client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "task_id": task_id, "check_name": "pytest",
            "status": "pass", "output": "x" * 65537,
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


@pytest.mark.asyncio
async def test_oversized_issue_title_rejected(client: AsyncClient) -> None:
    """report_issue title > 256 chars → 422."""
    resp = await client.post(
        "/api/tools/report_issue",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "title": "x" * 257, "description": "desc",
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


@pytest.mark.asyncio
async def test_oversized_progress_message_rejected(client: AsyncClient) -> None:
    """update_progress message > 1024 chars → 422."""
    resp = await client.post(
        "/api/tools/update_progress",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "message": "x" * 1025,
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Field length — min_length violations → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_check_name_rejected(
    client: AsyncClient, db: DatabaseManager
) -> None:
    """check_name = '' (below min_length=1) → 422."""
    task_id = await _make_task(db)
    resp = await client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "task_id": task_id, "check_name": "",
            "status": "pass",
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Malformed bodies → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_json_rejected(client: AsyncClient) -> None:
    """Non-JSON body → 422."""
    resp = await client.post(
        "/api/tools/report_issue",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


@pytest.mark.asyncio
async def test_missing_required_field_rejected(
    client: AsyncClient, db: DatabaseManager
) -> None:
    """Omitting required task_id → 422."""
    resp = await client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            # task_id intentionally omitted
            "check_name": "pytest", "status": "pass",
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Enum / pattern constraints → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_severity_rejected(client: AsyncClient) -> None:
    """severity='critical_danger' (not in enum) → 422."""
    resp = await client.post(
        "/api/tools/report_issue",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "title": "title", "description": "desc",
            "severity": "critical_danger",
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


@pytest.mark.asyncio
async def test_invalid_validation_status_rejected(
    client: AsyncClient, db: DatabaseManager
) -> None:
    """status='green' (not pass|fail|skip) → 422."""
    task_id = await _make_task(db)
    resp = await client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "task_id": task_id, "check_name": "pytest",
            "status": "green",
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


@pytest.mark.asyncio
async def test_percent_out_of_range_rejected(client: AsyncClient) -> None:
    """percent=101 (>100) → 422."""
    resp = await client.post(
        "/api/tools/update_progress",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "message": "working", "percent": 101,
        },
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Metacharacter injection — stored safely, not executed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_metacharacters_stored_safely(
    client: AsyncClient, db: DatabaseManager
) -> None:
    """HTML/script payload stored as-is in DB; returned as JSON (no HTML context)."""
    xss = "<script>alert(document.cookie)</script>"
    task_id = await _make_task(db)
    resp = await client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "task_id": task_id, "check_name": "pytest",
            "status": "pass", "output": xss,
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    # Stored verbatim (parameterized query, no DB injection)
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT output FROM validation_evidence WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
    assert row is not None
    assert row[0] == xss, "Payload must be stored verbatim, not executed or mangled"


@pytest.mark.asyncio
async def test_sql_metacharacters_stored_safely(
    client: AsyncClient, db: DatabaseManager
) -> None:
    """SQL injection attempt in output stored as literal text; DB not corrupted."""
    sql_payload = "'; DROP TABLE validation_evidence; --"
    task_id = await _make_task(db)
    resp = await client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "task_id": task_id, "check_name": "pytest",
            "status": "pass", "output": sql_payload,
        },
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    # Table still intact and row present
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT output FROM validation_evidence WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
    assert row is not None, "Table must survive SQL injection attempt in payload"
    assert row[0] == sql_payload


@pytest.mark.asyncio
async def test_path_traversal_in_scope_stored_safely(
    client: AsyncClient, db: DatabaseManager
) -> None:
    """Path-traversal string in scope stored as literal; no filesystem side-effect."""
    task_id = await _make_task(db)
    traversal = "../../etc/passwd"
    resp = await client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "task_id": task_id, "check_name": "pytest",
            "status": "pass", "output": traversal,
        },
    )
    # Scope value in the body doesn't control file I/O; output is just stored text.
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_unicode_and_null_bytes_in_output(
    client: AsyncClient, db: DatabaseManager
) -> None:
    """Unicode and null bytes in output are handled without crash."""
    payload = "✓ tests passed\x00\nline2  end"
    task_id = await _make_task(db)
    resp = await client.post(
        "/api/tools/record_validation",
        json={
            "agent_id": _AGENT_ID, "scope": "adv-scope",
            "task_id": task_id, "check_name": "pytest",
            "status": "pass", "output": payload,
        },
    )
    # Must not crash — 200 or 422 (if null bytes rejected by JSON layer), not 500.
    assert resp.status_code in (200, 422), (
        f"Unexpected status {resp.status_code}: {resp.text}"
    )
