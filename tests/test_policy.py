"""Tests for fleet/policy — Task 4.2 (TDD, written BEFORE implementation).

Run:
    uv run pytest tests/test_policy.py -q
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


def _default_manifest_path() -> str:
    import os

    return os.path.join(
        os.path.dirname(__file__),
        "..",
        "fleet",
        "manifests",
        "default.yaml",
    )


# ---------------------------------------------------------------------------
# 1. test_load_manifest_valid
# ---------------------------------------------------------------------------


def test_load_manifest_valid() -> None:
    """Load default.yaml; assert orchestrator has spawn_worker in allowed_tools."""
    from fleet.policy.rules import load_manifest

    manifest = load_manifest(_default_manifest_path())
    assert "orchestrator" in manifest.roles
    orchestrator = manifest.roles["orchestrator"]
    assert "spawn_worker" in orchestrator.allowed_tools


# ---------------------------------------------------------------------------
# 2. test_load_manifest_unknown_role_errors
# ---------------------------------------------------------------------------


def test_load_manifest_unknown_role_errors(tmp_path: Any) -> None:
    """A YAML with a malformed/required field missing should raise ValueError."""
    import yaml

    from fleet.policy.rules import load_manifest

    bad_yaml = {"version": "1"}  # missing required 'roles' field
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.dump(bad_yaml))

    with pytest.raises((ValueError, Exception)):
        load_manifest(str(p))


# ---------------------------------------------------------------------------
# 3. test_check_tool_allowed_passes
# ---------------------------------------------------------------------------


def test_check_tool_allowed_passes() -> None:
    """orchestrator + spawn_worker → no raise."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    # Must not raise
    svc.check_tool_allowed(role="orchestrator", tool_name="spawn_worker")


# ---------------------------------------------------------------------------
# 4. test_check_tool_allowed_denied_unknown_role
# ---------------------------------------------------------------------------


def test_check_tool_allowed_denied_unknown_role() -> None:
    """unknown_role + any_tool → PolicyDenied (fail-closed)."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyDenied, PolicyService

    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    with pytest.raises(PolicyDenied) as exc_info:
        svc.check_tool_allowed(role="unknown_role", tool_name="spawn_worker")
    assert exc_info.value.role == "unknown_role"


# ---------------------------------------------------------------------------
# 5. test_check_tool_allowed_denied_wrong_tool
# ---------------------------------------------------------------------------


def test_check_tool_allowed_denied_wrong_tool() -> None:
    """coder + spawn_worker → PolicyDenied (coders can't spawn)."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyDenied, PolicyService

    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    with pytest.raises(PolicyDenied) as exc_info:
        svc.check_tool_allowed(role="coder", tool_name="spawn_worker")
    assert exc_info.value.tool_name == "spawn_worker"
    assert exc_info.value.role == "coder"


# ---------------------------------------------------------------------------
# 6. test_check_secret_path_allows_normal_path
# ---------------------------------------------------------------------------


def test_check_secret_path_allows_normal_path() -> None:
    """src/main.py → no raise."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    # Must not raise
    svc.check_secret_path("src/main.py")


# ---------------------------------------------------------------------------
# 7. test_check_secret_path_denies_env_file
# ---------------------------------------------------------------------------


def test_check_secret_path_denies_env_file() -> None:
    """.env → PolicyDenied."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyDenied, PolicyService

    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    with pytest.raises(PolicyDenied):
        svc.check_secret_path(".env")


# ---------------------------------------------------------------------------
# 8. test_check_secret_path_denies_ssh_key
# ---------------------------------------------------------------------------


def test_check_secret_path_denies_ssh_key() -> None:
    """.ssh/id_rsa → PolicyDenied."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyDenied, PolicyService

    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    with pytest.raises(PolicyDenied):
        svc.check_secret_path(".ssh/id_rsa")


# ---------------------------------------------------------------------------
# 9. test_check_spawn_rate_allows_within_limits
# ---------------------------------------------------------------------------


def test_check_spawn_rate_allows_within_limits() -> None:
    """3 live workers, limit 10 → no raise."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    # Must not raise
    svc.check_spawn_rate(
        scope="scope-1",
        role="orchestrator",
        current_live_workers=3,
        spawns_last_minute=1,
    )


# ---------------------------------------------------------------------------
# 10. test_check_spawn_rate_denies_over_live_workers
# ---------------------------------------------------------------------------


def test_check_spawn_rate_denies_over_live_workers() -> None:
    """10 live workers, limit 10 → PolicyDenied."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyDenied, PolicyService

    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    with pytest.raises(PolicyDenied):
        svc.check_spawn_rate(
            scope="scope-1",
            role="orchestrator",
            current_live_workers=10,
            spawns_last_minute=1,
        )


# ---------------------------------------------------------------------------
# 11. test_check_spawn_rate_denies_over_per_minute
# ---------------------------------------------------------------------------


def test_check_spawn_rate_denies_over_per_minute() -> None:
    """5 spawns last minute, limit 5 → PolicyDenied."""
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyDenied, PolicyService

    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    with pytest.raises(PolicyDenied):
        svc.check_spawn_rate(
            scope="scope-1",
            role="orchestrator",
            current_live_workers=3,
            spawns_last_minute=5,
        )


# ---------------------------------------------------------------------------
# 12. test_api_tool_denied_returns_403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_tool_denied_returns_403() -> None:
    """POST /api/tools/spawn_worker as a coder agent → 403."""
    import os
    import tempfile
    from datetime import UTC, datetime

    from sqlalchemy import text

    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService
    from fleet.api.auth import require_token
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.db import init_db
    from fleet.events.service import EventService
    from fleet.events.sse import SSEHub
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    # Build a real in-memory DB with a coder agent seeded
    with tempfile.TemporaryDirectory() as td:
        db = await init_db(os.path.join(td, "policy_test.db"))
        hub = SSEHub()
        event_svc = EventService(db, hub)

        now = datetime.now(UTC).isoformat()
        agent_id = "agent-coder-1"

        def _seed(conn: Any) -> None:
            conn.execute(
                text(
                    "INSERT INTO agents"
                    " (id, name, scope, role, backend, model, status,"
                    "  created_at, updated_at)"
                    " VALUES (:id, :name, 'scope-1', 'coder', 'mock',"
                    "  'claude-sonnet-4-6', 'idle', :now, :now)"
                ),
                {"id": agent_id, "name": "coder-agent", "now": now},
            )
            conn.commit()

        await db.write(_seed)

        # Wire up agent service (need real one to do get_agent lookup)
        inbox_svc = InboxService(db)
        agent_svc = AgentService(db, event_svc, inbox_svc)

        # Wire policy service with the default manifest
        manifest = load_manifest(_default_manifest_path())
        policy_svc = PolicyService(manifest)

        set_tool_services(
            agent_svc=agent_svc,
            event_svc=event_svc,
            workspace_svc=None,
            worktree_svc=None,
            db=db,
        )
        set_policy_service(policy_svc)

        def _no_auth() -> None:
            return None

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_token] = _no_auth

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/tools/spawn_worker",
                json={
                    "agent_id": agent_id,
                    "scope": "scope-1",
                    "name": "new-worker",
                    "role": "coder",
                    "task_description": "do work",
                },
            )

        assert resp.status_code == 403
        # FastAPI wraps the HTTPException detail under "detail"
        outer = resp.json()
        body = outer.get("detail", outer)
        assert body.get("status") == 403
        assert body.get("type") == "policy_denied"

        await db.close()


# ---------------------------------------------------------------------------
# 13. test_api_tool_allowed_passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_tool_allowed_passes() -> None:
    """POST /api/tools/list_agents as observer → 200."""
    import os
    import tempfile
    from datetime import UTC, datetime

    from sqlalchemy import text

    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService
    from fleet.api.auth import require_token
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.db import init_db
    from fleet.events.service import EventService
    from fleet.events.sse import SSEHub
    from fleet.policy.rules import load_manifest
    from fleet.policy.service import PolicyService

    with tempfile.TemporaryDirectory() as td:
        db = await init_db(os.path.join(td, "policy_test2.db"))
        hub = SSEHub()
        event_svc = EventService(db, hub)

        now = datetime.now(UTC).isoformat()
        agent_id = "agent-observer-1"

        def _seed(conn: Any) -> None:
            conn.execute(
                text(
                    "INSERT INTO agents"
                    " (id, name, scope, role, backend, model, status,"
                    "  created_at, updated_at)"
                    " VALUES (:id, :name, 'scope-1', 'observer', 'mock',"
                    "  'claude-sonnet-4-6', 'idle', :now, :now)"
                ),
                {"id": agent_id, "name": "observer-agent", "now": now},
            )
            conn.commit()

        await db.write(_seed)

        inbox_svc = InboxService(db)
        agent_svc = AgentService(db, event_svc, inbox_svc)

        manifest = load_manifest(_default_manifest_path())
        policy_svc = PolicyService(manifest)

        set_tool_services(
            agent_svc=agent_svc,
            event_svc=event_svc,
            workspace_svc=None,
            worktree_svc=None,
            db=db,
        )
        set_policy_service(policy_svc)

        def _no_auth() -> None:
            return None

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_token] = _no_auth

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/tools/list_agents",
                json={
                    "agent_id": agent_id,
                    "scope": "scope-1",
                },
            )

        assert resp.status_code == 200

        await db.close()
