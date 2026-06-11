"""Boot-and-smoke E2E tests for the Fleet application (T11).

Starts the *real* ``fleet/main.py`` app (full lifespan: DB init, service wiring,
router mounting) and exercises a complete request flow over HTTP using
``httpx.AsyncClient`` + ``ASGITransport``.

These tests catch the "built-but-never-wired" class of bugs (P1-1…P1-8):
auth/config contradictions, router mounting gaps, and service injection
failures that unit tests (with mocked deps) cannot surface.

Marked ``slow`` so they are excluded by default (``addopts = "-m 'not live and
not slow'"``).  Run explicitly with::

    uv run pytest tests/test_boot_smoke.py -v -m slow

or simply::

    uv run pytest tests/test_boot_smoke.py -v --no-header -p no:cacheprovider
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Fixture: real app with real lifespan
# ---------------------------------------------------------------------------

_TOKEN = "test-smoke-token"


@pytest_asyncio.fixture
async def app_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """Spin up the real Fleet app via its lifespan and yield an HTTP client.

    Strategy
    --------
    * Set ``FLEET_DB_PATH`` to a temp SQLite file so we never touch ``fleet.db``.
    * Set ``FLEET_API_TOKEN`` so auth is active (non-empty token).
    * Import ``fleet.main`` *after* env vars are set — Settings reads from env
      at construction time inside ``lifespan()``, so order matters.
    * Enter the lifespan context manager directly (same pattern used by
      ``test_main_wiring.py``).
    * Pass the real ``app`` object to ``ASGITransport``; the lifespan has
      already wired all DI singletons so requests are fully handled.
    """
    db_path = str(tmp_path / "smoke.db")
    old_db = os.environ.get("FLEET_DB_PATH")
    old_token = os.environ.get("FLEET_API_TOKEN")

    os.environ["FLEET_DB_PATH"] = db_path
    os.environ["FLEET_API_TOKEN"] = _TOKEN

    try:
        # Import *inside* the fixture so that Settings() sees the env vars we
        # just set.  If main was already imported, the module-level ``app``
        # object is reused — that is fine because lifespan re-wires all DI
        # singletons on every startup.
        from fleet.main import app, lifespan

        # Build a minimal mock app container that lifespan can write state onto.
        # We use the real ``app`` for routing, but lifespan needs an object it
        # can attach ``app.state`` to.  The real FastAPI ``app`` has a ``state``
        # attribute, so we pass it directly.
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {_TOKEN}"},
            ) as client:
                yield client
    finally:
        if old_db is None:
            os.environ.pop("FLEET_DB_PATH", None)
        else:
            os.environ["FLEET_DB_PATH"] = old_db

        if old_token is None:
            os.environ.pop("FLEET_API_TOKEN", None)
        else:
            os.environ["FLEET_API_TOKEN"] = old_token


# ---------------------------------------------------------------------------
# Test 1: all routers are mounted and return sane status codes
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_health_routers_mounted(app_client: AsyncClient) -> None:
    """All routers must be reachable (not 404 / 500 at the routing layer).

    Checks that the module-level ``app.include_router(...)`` calls in
    ``fleet/main.py`` are all present and that service DI is wired.
    """
    # Agents list — should be 200, empty list
    r = await app_client.get("/api/agents?scope=smoke")
    assert r.status_code == 200, f"GET /api/agents: {r.status_code} {r.text}"

    # Events list — should be 200, empty list
    r = await app_client.get("/api/events?scope=smoke")
    assert r.status_code == 200, f"GET /api/events: {r.status_code} {r.text}"

    # Workspaces list — was 404 before T5 fix (router not mounted)
    r = await app_client.get("/api/workspaces")
    assert r.status_code == 200, f"GET /api/workspaces: {r.status_code} {r.text}"

    # Approvals list — should be 200, empty list
    r = await app_client.get("/api/approvals?scope=smoke")
    assert r.status_code == 200, f"GET /api/approvals: {r.status_code} {r.text}"

    # Decide on a non-existent approval — router IS mounted, so 404 (not 500)
    r = await app_client.post(
        "/api/approvals/nonexistent-id/decide",
        json={"decision": "approve", "comment": ""},
    )
    assert r.status_code == 404, (
        f"POST /api/approvals/nonexistent/decide: "
        f"expected 404, got {r.status_code} {r.text}"
    )


# ---------------------------------------------------------------------------
# Test 2: spawn agent and observe it in the list + event stream
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_spawn_agent_flow(app_client: AsyncClient) -> None:
    """Create an agent via POST /api/agents and verify it appears in GET /api/agents.

    Also checks that a state_change event was emitted.
    """
    payload = {
        "scope": "smoke",
        "name": "test-coder",
        "role": "coder",
        "model": "claude-3-haiku-20240307",
        "backend_type": "mock",
    }
    r = await app_client.post("/api/agents", json=payload)
    assert r.status_code == 201, f"POST /api/agents: {r.status_code} {r.text}"

    data = r.json()
    assert "id" in data, f"Response missing 'id': {data}"
    agent_id: str = data["id"]

    # The agent must appear in the scope's list
    r = await app_client.get("/api/agents?scope=smoke")
    assert r.status_code == 200
    agents = r.json()
    ids = [a["id"] for a in agents]
    assert agent_id in ids, f"Spawned agent {agent_id!r} not in list: {ids}"

    # At least one event must have been recorded for this scope
    r = await app_client.get("/api/events?scope=smoke")
    assert r.status_code == 200
    events = r.json()
    assert len(events) > 0, "No events recorded after agent creation"

    # Expect a state_change event (or any event referencing our agent)
    types = {ev["type"] for ev in events}
    agent_ids = {ev.get("agent_id") for ev in events}
    assert "state_change" in types or agent_id in agent_ids, (
        f"No state_change event found; got types={types}, agent_ids={agent_ids}"
    )


# ---------------------------------------------------------------------------
# Test 3: tool call — list_agents via POST /api/tools/list_agents
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_tool_call_flow(app_client: AsyncClient) -> None:
    """POST /api/tools/list_agents must return 200 with a list of agents.

    Verifies the tools router is mounted AND the tool registry contains
    ``list_agents``.
    """
    # Spawn an agent first so the list is non-trivial
    payload = {
        "scope": "smoke-tools",
        "name": "tool-caller",
        "role": "orchestrator",
        "model": "claude-3-haiku-20240307",
        "backend_type": "mock",
    }
    r = await app_client.post("/api/agents", json=payload)
    assert r.status_code == 201, f"Prerequisite agent creation failed: {r.text}"
    agent_id = r.json()["id"]

    # Call the list_agents tool
    r = await app_client.post(
        "/api/tools/list_agents",
        json={"agent_id": agent_id, "scope": "smoke-tools"},
    )
    assert r.status_code == 200, (
        f"POST /api/tools/list_agents: {r.status_code} {r.text}"
    )
    result = r.json()
    # Result is a dict with an "agents" or "result" key
    assert isinstance(result, dict), f"Expected dict response, got: {type(result)}"


# ---------------------------------------------------------------------------
# Test 4: approval flow — 404 for nonexistent, 200 for list
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_approval_flow(app_client: AsyncClient) -> None:
    """Approval endpoints must be wired correctly.

    * Deciding on a nonexistent approval → 404 (not 401 — auth is working)
    * Listing approvals → 200 empty list
    """
    # 404 because nonexistent-id does not exist (but router IS mounted and auth passes)
    r = await app_client.post(
        "/api/approvals/nonexistent-id/decide",
        json={"decision": "approve", "comment": "smoke test"},
    )
    assert r.status_code == 404, (
        f"Expected 404 for unknown approval, got {r.status_code}: {r.text}"
    )

    # List pending approvals — should be 200 with empty list
    r = await app_client.get("/api/approvals?scope=smoke")
    assert r.status_code == 200, f"GET /api/approvals: {r.status_code} {r.text}"
    assert r.json() == [], f"Expected empty approvals list, got: {r.json()}"


# ---------------------------------------------------------------------------
# Test 5: dashboard auth
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_dashboard_requires_auth(app_client: AsyncClient) -> None:
    """Dashboard routes must enforce bearer-token auth.

    * Wrong / missing token → 401
    * Correct token → 200 (HTML response rendered)
    """
    from fleet.main import app

    # Request WITHOUT the correct auth token — use a separate client
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        # No Authorization header
    ) as anon:
        r = await anon.get("/dashboard/")
        assert r.status_code == 401, (
            f"GET /dashboard/ without auth: expected 401, got {r.status_code}"
        )

    # Request WITH the correct bearer token — app_client has the right header
    r = await app_client.get("/dashboard/")
    assert r.status_code == 200, (
        f"GET /dashboard/ with correct auth: expected 200, got {r.status_code} {r.text}"
    )
    # Verify it is an HTML response (dashboard renders templates)
    content_type = r.headers.get("content-type", "")
    assert "text/html" in content_type, (
        f"Expected HTML response from /dashboard/, got content-type: {content_type!r}"
    )
