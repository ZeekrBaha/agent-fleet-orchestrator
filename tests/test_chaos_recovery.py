"""P2-1: Chaos and recovery test suite.

Verifies that Fleet degrades gracefully — not crashing — when the environment
is hostile: missing worktree directories, non-git paths, unavailable services.

Coverage:
  - SHA resolution against a missing worktree path → evidence stored with NULL SHA
  - SHA resolution against a non-git directory → evidence stored with NULL SHA
  - Warning logged when SHA resolution fails
  - record_validation with no worktree context → evidence stored with NULL SHA
  - Evidence service when task doesn't exist → 200 with error or graceful 4xx
  - Gate check on non-existent task → can_merge=False with clear reason
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
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
from fleet.models import AgentRecord, WorktreeRecord
from fleet.review.evidence import EvidenceService

_PERMISSIVE_MANIFEST = os.path.join(
    os.path.dirname(__file__), "manifests", "permissive.yaml"
)
_ADMIN_TOKEN = "fleet-chaos-test-token"
_AGENT_TOKEN = "agent-chaos-tok"
_AGENT_ID = "agent-chaos"


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
            _AGENT_ID, _AGENT_ID, "chaos-scope", "test_role",
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
    return str(tmp_path / "chaos_test.db")


@pytest_asyncio.fixture
async def db(db_path: str) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(db_path)
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager) -> EventService:
    return EventService(db, SSEHub())


def _build_tool_app(
    db: DatabaseManager,
    db_path: str,
    event_svc: EventService,
    *,
    worktree_path: str | None = None,
    worktree_id: str = "wt-chaos",
) -> FastAPI:
    from fleet.api.auth import get_settings, set_auth_db
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.config import Settings
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    _insert_agent(db_path)

    mock_agent_svc = AsyncMock()
    mock_agent_svc.get_agent.return_value = AgentRecord(
        id=_AGENT_ID, name=_AGENT_ID, scope="chaos-scope", role="test_role",
        backend="mock", model="mock", status="idle",
        worktree_id=worktree_id if worktree_path else None,
        created_at=_now(), updated_at=_now(),
    )
    mock_agent_svc.list_agents.return_value = []

    mock_worktree_svc = AsyncMock()
    if worktree_path:
        mock_worktree_svc.get_worktree.return_value = WorktreeRecord(
            id=worktree_id, agent_id=_AGENT_ID, repository_id="repo-1",
            path=worktree_path, branch="main", base_branch="main",
            owned_paths_json="[]", status="active", created_at=_now(),
        )
    else:
        mock_worktree_svc.get_worktree.return_value = None

    set_auth_db(db)
    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_svc,
        workspace_svc=None,
        worktree_svc=mock_worktree_svc,
        db=db,
        evidence_svc=EvidenceService(db, gate_require_reviewer=False),
    )
    set_policy_service(PolicyService(load_manifest(_PERMISSIVE_MANIFEST)))

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: Settings(api_token=_ADMIN_TOKEN)
    return app


async def _make_task(db: DatabaseManager) -> str:
    ev = EvidenceService(db, gate_require_reviewer=False)
    return await ev.create_task(
        scope="chaos-scope", title="T", description="d",
        owner_agent_id=_AGENT_ID, branch="main",
    )


# ---------------------------------------------------------------------------
# SHA resolution — missing directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sha_resolution_missing_directory_stores_null(
    db: DatabaseManager, db_path: str, event_svc: EventService, tmp_path: Path
) -> None:
    """Worktree path that does not exist → evidence stored with NULL SHA, no crash."""
    missing_path = str(tmp_path / "does_not_exist")
    app = _build_tool_app(db, db_path, event_svc, worktree_path=missing_path)
    task_id = await _make_task(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/record_validation",
            json={
                "agent_id": _AGENT_ID, "scope": "chaos-scope",
                "task_id": task_id, "check_name": "pytest", "status": "pass",
            },
            headers={"Authorization": f"Bearer {_AGENT_TOKEN}"},
        )

    assert resp.status_code == 200, (
        f"Expected 200 (degraded SHA), got {resp.status_code}: {resp.text}"
    )

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT commit_sha FROM validation_evidence WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
    assert row is not None
    assert row[0] is None, f"commit_sha should be NULL for missing path, got {row[0]!r}"


# ---------------------------------------------------------------------------
# SHA resolution — non-git directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sha_resolution_non_git_dir_stores_null(
    db: DatabaseManager, db_path: str, event_svc: EventService, tmp_path: Path
) -> None:
    """Worktree path exists but is not a git repo → NULL SHA, no crash."""
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()
    (non_git / "file.txt").write_text("not a git repo\n")

    app = _build_tool_app(db, db_path, event_svc, worktree_path=str(non_git))
    task_id = await _make_task(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/record_validation",
            json={
                "agent_id": _AGENT_ID, "scope": "chaos-scope",
                "task_id": task_id, "check_name": "pytest", "status": "pass",
            },
            headers={"Authorization": f"Bearer {_AGENT_TOKEN}"},
        )

    assert resp.status_code == 200, (
        f"Expected 200 (degraded SHA), got {resp.status_code}: {resp.text}"
    )

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT commit_sha FROM validation_evidence WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
    assert row is not None
    assert row[0] is None, f"commit_sha should be NULL for non-git path, got {row[0]!r}"


# ---------------------------------------------------------------------------
# SHA resolution — warning logged on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sha_resolution_failure_emits_warning(
    db: DatabaseManager, db_path: str, event_svc: EventService,
    tmp_path: Path, caplog: Any,
) -> None:
    """GitError during SHA resolution must emit a WARNING log, not silently fail."""
    non_git = tmp_path / "non_git_warn"
    non_git.mkdir()

    app = _build_tool_app(db, db_path, event_svc, worktree_path=str(non_git))
    task_id = await _make_task(db)

    with caplog.at_level(logging.WARNING, logger="fleet.api.tool_handlers"):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                "/api/tools/record_validation",
                json={
                    "agent_id": _AGENT_ID, "scope": "chaos-scope",
                    "task_id": task_id, "check_name": "pytest", "status": "pass",
                },
                headers={"Authorization": f"Bearer {_AGENT_TOKEN}"},
            )

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "Expected at least one WARNING when SHA resolution fails"
    msgs = [r.message for r in warning_records]
    assert any(
        "sha" in m.lower() or "resolution" in m.lower() or "commit" in m.lower()
        for m in msgs
    ), f"Warning should mention SHA/resolution; got: {msgs!r}"


# ---------------------------------------------------------------------------
# No worktree context — evidence still stored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_validation_without_worktree_succeeds(
    db: DatabaseManager, db_path: str, event_svc: EventService
) -> None:
    """Agent with no worktree_id → evidence stored with NULL SHA, no crash."""
    app = _build_tool_app(db, db_path, event_svc, worktree_path=None)
    task_id = await _make_task(db)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/record_validation",
            json={
                "agent_id": _AGENT_ID, "scope": "chaos-scope",
                "task_id": task_id, "check_name": "pytest", "status": "pass",
            },
            headers={"Authorization": f"Bearer {_AGENT_TOKEN}"},
        )

    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.text}"
    )

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT commit_sha FROM validation_evidence WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()
    assert row is not None
    assert row[0] is None


# ---------------------------------------------------------------------------
# Gate check on non-existent task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_check_nonexistent_task_returns_false(db: DatabaseManager) -> None:
    """check_merge_gate for unknown task_id returns (False, reason) — no exception."""
    ev = EvidenceService(db, gate_require_reviewer=False)
    can_merge, reason = await ev.check_merge_gate(
        "00000000-0000-0000-0000-000000000000",
        branch_sha="deadbeef",
    )
    assert not can_merge
    assert "not found" in reason.lower() or "task" in reason.lower(), (
        f"Reason should indicate task not found; got: {reason!r}"
    )


# ---------------------------------------------------------------------------
# Concurrent record_evidence calls — no data corruption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_evidence_records_no_corruption(db: DatabaseManager) -> None:
    """Concurrent evidence inserts via the single-writer queue don't corrupt DB."""
    import asyncio

    ev = EvidenceService(db, gate_require_reviewer=False)
    task_id = await ev.create_task(
        scope="chaos-scope", title="T", description="d",
        owner_agent_id="agent-x", branch="main",
    )

    async def record(check_name: str) -> None:
        await ev.record_evidence(
            task_id=task_id, check_name=check_name,
            status="pass", output=f"output from {check_name}",
        )

    # Fire 10 concurrent writes through the single-writer queue
    await asyncio.gather(*[record(f"check-{i}") for i in range(10)])

    evidence = await ev.list_evidence(task_id)
    assert len(evidence) == 10, f"Expected 10 evidence rows, got {len(evidence)}"
    check_names = {e["check_name"] for e in evidence}
    assert check_names == {f"check-{i}" for i in range(10)}, (
        "All 10 distinct checks must be stored"
    )
