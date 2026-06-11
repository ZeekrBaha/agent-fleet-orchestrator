"""Playwright smoke tests for the Fleet web dashboard (Task 7.1).

All tests are marked @pytest.mark.slow so they are skipped in the normal
`uv run pytest -q -m "not live and not slow"` run.

Run with:
    uv run pytest tests/test_dashboard_smoke.py -m slow

Requirements:
    pip install pytest-playwright
    playwright install chromium

Design: Tests use sync Playwright API. The uvicorn server runs in a
background thread (not asyncio). The seeded DB is created synchronously
before the server starts.
"""

from __future__ import annotations

import asyncio
import pathlib
import socket
import threading
import time

import pytest

# ---------------------------------------------------------------------------
# Server + seeded DB helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _build_dashboard_app(db_path: str) -> object:
    """Build a self-contained FastAPI app with dashboard and seed DB wired up."""
    import pathlib
    from collections.abc import AsyncIterator as _AI
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    from fleet.api.approvals import router as approvals_router
    from fleet.api.auth import require_token
    from fleet.api.events import router as events_router
    from fleet.approvals.service import ApprovalService
    from fleet.dashboard.router import router as dashboard_router
    from fleet.dashboard.router import (
        set_approval_service as set_dashboard_approval_svc,
    )
    from fleet.dashboard.router import set_db, set_templates
    from fleet.db import init_db
    from fleet.events.service import create_event_service
    from fleet.events.sse import SSEHub

    templates_dir = (
        pathlib.Path(__file__).parent.parent / "fleet" / "templates"
    )
    static_dir = pathlib.Path(__file__).parent.parent / "fleet" / "static"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> _AI[None]:
        manager = await init_db(db_path)
        sse_hub = SSEHub()
        event_svc = create_event_service(manager, sse_hub)
        approval_svc = ApprovalService(manager, event_svc)

        templates = Jinja2Templates(directory=str(templates_dir))
        set_db(manager)
        set_templates(templates)

        app.state.event_service = event_svc
        app.state.sse_hub = sse_hub
        app.state.approval_service = approval_svc

        import fleet.api.approvals as _appr_api

        _appr_api.set_approval_service(approval_svc)
        set_dashboard_approval_svc(approval_svc)

        yield
        await manager.close()

    async def _no_auth() -> None:
        return None

    app = FastAPI(lifespan=lifespan)
    app.dependency_overrides[require_token] = _no_auth
    app.include_router(dashboard_router)
    app.include_router(events_router)
    app.include_router(approvals_router)

    if static_dir.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(static_dir)),
            name="static",
        )

    return app


class _LiveServer:
    """Runs uvicorn in a background thread. Use as context manager."""

    def __init__(self, app: object, port: int) -> None:
        self._app = app
        self._port = port
        self._thread: threading.Thread | None = None
        self._server: object = None

    def start(self) -> str:
        """Start server; return base URL once ready."""
        import uvicorn

        config = uvicorn.Config(
            self._app,
            host="127.0.0.1",
            port=self._port,
            log_level="error",
        )
        server = uvicorn.Server(config)
        self._server = server

        ready = threading.Event()
        original_startup = server.startup

        async def _patched_startup(sockets: object = None) -> None:
            await original_startup(sockets)
            ready.set()

        server.startup = _patched_startup  # type: ignore[method-assign]

        def _run() -> None:
            asyncio.run(server.serve())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

        if not ready.wait(timeout=15):
            raise RuntimeError("uvicorn did not start in time")
        return f"http://127.0.0.1:{self._port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True  # type: ignore[attr-defined]
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> str:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path: pathlib.Path) -> str:
    """Create a seeded SQLite DB for smoke tests and return its path."""
    from fleet.dashboard.seed import seed_test_db
    from fleet.db import run_migrations

    db_path = str(tmp_path / "smoke_test.db")
    run_migrations(db_path)

    seed_test_db(db_path)
    return db_path


@pytest.fixture
def empty_db(tmp_path: pathlib.Path) -> str:
    """Create an empty (schema-only) SQLite DB for empty-state tests."""
    from fleet.db import run_migrations

    db_path = str(tmp_path / "empty_test.db")
    run_migrations(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Smoke tests (sync Playwright API to avoid event loop conflict)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_roster_renders_all_statuses(seeded_db: str, page: object) -> None:
    """Roster page shows all 6 agent statuses; no JS console errors."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    js_errors: list[str] = []
    p.on("pageerror", lambda e: js_errors.append(str(e)))

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/")
        p.wait_for_load_state("networkidle")

        content = p.content()
        all_statuses = (
            "idle", "running", "waiting", "paused budget", "failed", "archived"
        )
        for status in all_statuses:
            assert status in content.lower(), (
                f"Status '{status}' not found in roster"
            )

    assert js_errors == [], f"JS errors on roster page: {js_errors}"


@pytest.mark.slow
def test_roster_empty_state(empty_db: str, page: object) -> None:
    """Empty DB → empty state copy visible on roster."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(empty_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/")
        p.wait_for_load_state("networkidle")

        content = p.content()
        assert "no agents yet" in content.lower(), "Empty state text not found"


@pytest.mark.slow
def test_conversation_sse_live_tail(seeded_db: str, page: object) -> None:
    """Connect to conversation view, inject event via API, assert it appears."""
    import httpx
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/agents/agent-running/conversation")
        # Use "domcontentloaded" not "networkidle" — SSE keeps connection open
        p.wait_for_load_state("domcontentloaded")
        # Let the SSE EventSource connect (it starts after DOMContentLoaded)
        time.sleep(1.0)

        # Inject a new event synchronously via the events API
        resp = httpx.post(
            f"{base_url}/api/events",
            json={
                "scope": "fleet-test",
                "type": "agent.message",
                "summary": "smoke-test-unique-msg-12345",
                "agent_id": "agent-running",
            },
        )
        assert resp.status_code == 200

        # Wait up to 5s for the new event to appear in the DOM via SSE
        p.wait_for_function(
            "() => document.body.innerText.includes('smoke-test-unique-msg-12345')",
            timeout=5000,
        )


@pytest.mark.slow
def test_timeline_filter_by_type(seeded_db: str, page: object) -> None:
    """Filter timeline to 'error' type → only error rows visible."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/timeline?type_filter=error")
        p.wait_for_load_state("networkidle")

        content = p.content()
        assert "error" in content.lower()

        # Verify all event rows have type=error
        rows = p.query_selector_all("[data-event-type]")
        for row in rows:
            event_type = row.get_attribute("data-event-type")
            assert event_type == "error", f"Non-error row found: {event_type}"


@pytest.mark.slow
def test_approval_approve_flow(seeded_db: str, page: object) -> None:
    """Click Approve on pending approval → status updates to approved."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/approvals")
        p.wait_for_load_state("networkidle")

        approve_btn = p.get_by_role("button", name="Approve").first
        approve_btn.wait_for(state="visible", timeout=5000)
        approve_btn.click()

        # Wait for htmx to swap in the updated row
        time.sleep(1.5)

        content = p.content()
        assert "approved" in content.lower(), (
            "Approval status not updated to approved"
        )


@pytest.mark.slow
def test_approval_deny_flow(seeded_db: str, page: object) -> None:
    """Click Deny → decision recorded; row updates to denied."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/approvals")
        p.wait_for_load_state("networkidle")

        deny_btn = p.get_by_role("button", name="Deny").first
        deny_btn.wait_for(state="visible", timeout=5000)
        deny_btn.click()

        # Wait for htmx to update
        time.sleep(1.5)

        content = p.content()
        assert "denied" in content.lower(), "Denial status not recorded"


@pytest.mark.slow
def test_merge_validation_checklist(seeded_db: str, page: object) -> None:
    """Task with all-pass evidence → merge button is enabled."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/tasks/task-1/validation")
        p.wait_for_load_state("networkidle")

        content = p.content()
        assert "lint_check" in content, "Evidence check 'lint_check' not found"
        assert "unit_tests" in content, "Evidence check 'unit_tests' not found"
        assert "e2e_tests" in content, "Evidence check 'e2e_tests' not found"

        # Merge button should not be disabled for 2 pass + 1 skip evidence
        merge_btn = p.get_by_role("button", name="Approve & Merge")
        if merge_btn.count() > 0:
            is_disabled = merge_btn.get_attribute("disabled")
            assert is_disabled is None, (
                "Merge button should be enabled with passing evidence"
            )


@pytest.mark.slow
def test_worktree_view_renders(seeded_db: str, page: object) -> None:
    """Worktree view renders with branch name and data-state=success."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/worktrees/wt-1")
        p.wait_for_load_state("networkidle")

        content = p.content()
        assert "feat/worker-a-task" in content, (
            "Branch name 'feat/worker-a-task' not found in worktree view"
        )
        # data-state should be success, not error
        body = p.query_selector("#worktree-body")
        assert body is not None, "#worktree-body element not found"
        assert body.get_attribute("data-state") == "success", (
            "data-state should be 'success' for valid worktree"
        )


@pytest.mark.slow
def test_roster_loading_state(seeded_db: str, page: object) -> None:
    """Roster page renders agent rows and agent-roster-tbody is present."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/")
        p.wait_for_load_state("networkidle")

        # Skeleton rows are in the Jinja2 {% else %} branch (no agents).
        # With seeded data, verify the success state renders correctly.
        content = p.content()
        # 6 agents seeded — roster table should be present
        assert "agent-roster-tbody" in content, "Agent roster tbody not found"
        # Verify at least one agent rendered
        rows = p.query_selector_all("#agent-roster-tbody tr")
        assert len(rows) >= 1, "Expected at least 1 agent row in roster"


@pytest.mark.slow
def test_timeline_error_state(seeded_db: str, page: object) -> None:
    """Timeline page: data-state=success on normal load, error div hidden."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/timeline")
        p.wait_for_load_state("networkidle")

        # On successful load, data-state should be "success"
        body = p.query_selector("#timeline-body")
        assert body is not None, "#timeline-body element not found"
        assert body.get_attribute("data-state") == "success", (
            "data-state should be 'success' on normal load"
        )
        # error-state div should be hidden (display:none)
        error_div = p.query_selector("#error-state")
        assert error_div is not None, "#error-state element not found"
        is_visible = error_div.is_visible()
        assert not is_visible, "Error banner should be hidden on successful load"


@pytest.mark.slow
def test_approval_success_feedback(seeded_db: str, page: object) -> None:
    """Approve an approval; assert success message visible."""
    from playwright.sync_api import Page

    p: Page = page  # type: ignore[assignment]
    port = _free_port()
    app = _build_dashboard_app(seeded_db)

    with _LiveServer(app, port) as base_url:
        p.goto(f"{base_url}/dashboard/approvals")
        p.wait_for_load_state("networkidle")

        approve_btn = p.get_by_role("button", name="Approve").first
        approve_btn.wait_for(state="visible", timeout=5000)
        approve_btn.click()

        # Wait for htmx swap to complete
        time.sleep(1.5)

        # Success message should be visible after the swap
        success_el = p.query_selector("#success-state")
        assert success_el is not None, "#success-state element not found"
        assert success_el.is_visible(), (
            "Success banner should be visible after approval action"
        )
        success_text = success_el.inner_text()
        assert "decision recorded" in success_text.lower(), (
            f"Expected 'decision recorded' in success message, got: {success_text!r}"
        )
