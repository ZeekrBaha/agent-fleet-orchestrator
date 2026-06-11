"""P0-3 wiring: end-to-end SHA stamping via the record_validation tool path.

Two gaps remained after the initial P0-3 fix:

1. check_merge_gate skips NULL-SHA evidence rows even when branch_sha is
   known, so an agent can record NULL-sha evidence (the current bug), push
   new commits, and merge — bypassing the staleness guard entirely.

2. _handle_record_validation never resolves or passes commit_sha to
   record_evidence, so every real evidence row written via the API tool
   path has commit_sha = NULL.

These tests drive both fixes:
  a. NULL-SHA evidence must block the gate when branch_sha is provided.
  b. The record_validation tool path must stamp evidence with the calling
     agent's worktree HEAD SHA (server-resolved, not caller-supplied).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
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
from fleet.models import AgentRecord, WorktreeRecord
from fleet.review.evidence import EvidenceService

_PERMISSIVE_MANIFEST = os.path.join(
    os.path.dirname(__file__), "manifests", "permissive.yaml"
)
_ADMIN_TOKEN = "fleet-wiring-test-token"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _make_git_repo(path: Path) -> None:
    def _git(args: list[str]) -> None:
        subprocess.run(["git"] + args, cwd=path, check=True, capture_output=True)

    _git(["init", "-b", "main"])
    _git(["config", "user.email", "test@fleet.local"])
    _git(["config", "user.name", "Fleet Test"])
    (path / "README.md").write_text("# test\n")
    _git(["add", "README.md"])
    _git(["commit", "-m", "initial"])


def _git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _insert_agent(
    db_path: str,
    *,
    agent_id: str,
    role: str = "test_role",
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "wiring_test.db")


@pytest_asyncio.fixture
async def db(db_path: str) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(db_path)
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def event_svc(db: DatabaseManager) -> EventService:
    return EventService(db, SSEHub())


@pytest_asyncio.fixture
async def evidence_svc(db: DatabaseManager) -> EvidenceService:
    return EvidenceService(db, gate_require_reviewer=False)


@pytest.fixture
def worktree_path(tmp_path: Path) -> Path:
    path = tmp_path / "wt"
    path.mkdir()
    _make_git_repo(path)
    return path


@pytest_asyncio.fixture
async def tool_app(
    db: DatabaseManager,
    event_svc: EventService,
    worktree_path: Path,
) -> FastAPI:
    """FastAPI app wired with real evidence_svc and mocked agent/worktree services."""
    from fleet.api.auth import get_settings, set_auth_db
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.config import Settings
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    mock_agent_svc = AsyncMock()
    mock_agent_svc.get_agent.return_value = AgentRecord(
        id="worker-wt",
        name="worker-wt",
        scope="test-scope",
        role="test_role",
        backend="mock",
        model="mock",
        status="idle",
        worktree_id="wt-abc",
        created_at=_now(),
        updated_at=_now(),
    )

    mock_worktree_svc = AsyncMock()
    mock_worktree_svc.get_worktree.return_value = WorktreeRecord(
        id="wt-abc",
        agent_id="worker-wt",
        repository_id="repo-1",
        path=str(worktree_path),
        branch="main",
        base_branch="main",
        owned_paths_json="[]",
        status="active",
        created_at=_now(),
    )

    ev_svc_for_handler = EvidenceService(db, gate_require_reviewer=False)

    set_auth_db(db)
    set_tool_services(
        agent_svc=mock_agent_svc,
        event_svc=event_svc,
        workspace_svc=None,
        worktree_svc=mock_worktree_svc,
        db=db,
        evidence_svc=ev_svc_for_handler,
    )
    set_policy_service(PolicyService(load_manifest(_PERMISSIVE_MANIFEST)))

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: Settings(api_token=_ADMIN_TOKEN)
    return app


# ---------------------------------------------------------------------------
# Test 1: Gate NULL-SHA policy (service layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_sha_evidence_blocked_when_branch_sha_known(
    evidence_svc: EvidenceService,
) -> None:
    """check_merge_gate must reject NULL-SHA evidence when branch_sha is provided.

    The current bug: NULL commit_sha rows are skipped by the staleness check
    (condition `is not None`), so they silently pass.  After the fix, any row
    with commit_sha=NULL is treated as unbound/stale when the branch SHA is known.
    """
    task_id = await evidence_svc.create_task(
        scope="test",
        title="T",
        description="desc",
        owner_agent_id="agent-1",
        branch="main",
    )
    await evidence_svc.record_evidence(
        task_id=task_id,
        check_name="pytest",
        status="pass",
        commit_sha=None,
    )

    can_merge, reason = await evidence_svc.check_merge_gate(
        task_id, branch_sha="deadbeefdeadbeef"
    )

    assert not can_merge, "NULL-SHA evidence must block gate when branch_sha is known"
    assert any(
        word in reason.lower() for word in ("stale", "null", "unbound", "sha")
    ), f"Reason should describe the SHA issue; got: {reason!r}"


# ---------------------------------------------------------------------------
# Test 2: Tool path stamps commit_sha (end-to-end API layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_validation_tool_stamps_commit_sha(
    db: DatabaseManager,
    db_path: str,
    tool_app: FastAPI,
    worktree_path: Path,
) -> None:
    """record_validation tool path stamps evidence with the agent's worktree HEAD SHA.

    Server-side: handler reads _calling_agent.worktree_id, resolves the
    worktree path, runs git rev-parse HEAD, and stores the result in the
    evidence row.  The caller does not supply the SHA.
    """
    expected_sha = _git_head(worktree_path)

    _insert_agent(
        db_path, agent_id="worker-wt", role="test_role", plaintext_token="wt-tok"
    )

    ev_svc = EvidenceService(db, gate_require_reviewer=False)
    task_id = await ev_svc.create_task(
        scope="test-scope",
        title="T",
        description="desc",
        owner_agent_id="worker-wt",
        branch="main",
    )

    async with AsyncClient(
        transport=ASGITransport(app=tool_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/record_validation",
            json={
                "agent_id": "worker-wt",
                "scope": "test-scope",
                "task_id": task_id,
                "check_name": "pytest",
                "status": "pass",
                "output": "all green",
            },
            headers={"Authorization": "Bearer wt-tok"},
        )

    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.text}"
    )

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT commit_sha FROM validation_evidence WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()

    assert row is not None, "No evidence row found in DB"
    assert row[0] == expected_sha, (
        f"Expected commit_sha={expected_sha!r}, got {row[0]!r}"
    )
