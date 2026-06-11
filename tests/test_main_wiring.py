"""Tests for T5: main.py lifespan wiring fixes.

Bugs covered:
    P1-1  — restore_sessions() never called on startup
    P1-2  — workspaces router never mounted (10 endpoints return 404)
    P1-22 — no graceful shutdown (stop_all never called)

TDD: tests are written first. They FAIL before the fixes below are applied.
"""
from __future__ import annotations

import inspect
import os
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fleet.agents.service import AgentService
from fleet.main import app, lifespan

# ---------------------------------------------------------------------------
# P1-1: restore_sessions must be called on startup
# ---------------------------------------------------------------------------


def test_restore_sessions_method_exists() -> None:
    """AgentService must expose an async restore_sessions method."""
    assert hasattr(AgentService, "restore_sessions"), (
        "AgentService is missing restore_sessions"
    )
    assert inspect.iscoroutinefunction(AgentService.restore_sessions), (
        "AgentService.restore_sessions must be a coroutine function"
    )


async def test_lifespan_calls_restore_sessions(tmp_path: pathlib.Path) -> None:
    """Lifespan startup must await agent_svc.restore_sessions()."""
    os.environ["FLEET_DB_PATH"] = str(tmp_path / "wiring_restore.db")
    called: list[str] = []

    async def _spy_restore(self, backends=None) -> None:
        called.append("restore_sessions")

    # Use a minimal mock app so the lifespan doesn't touch the real FastAPI singleton.
    mock_app = MagicMock()
    mock_app.state = SimpleNamespace()

    try:
        with patch.object(AgentService, "restore_sessions", _spy_restore):
            async with lifespan(mock_app):
                pass  # lifespan startup ran; yield; then teardown
    finally:
        os.environ.pop("FLEET_DB_PATH", None)

    assert "restore_sessions" in called, (
        "restore_sessions was not called during lifespan startup"
    )


# ---------------------------------------------------------------------------
# P1-2: workspaces router must be mounted
# ---------------------------------------------------------------------------


def test_workspaces_router_paths_registered() -> None:
    """app must include at least one /api/workspaces route (mounted at module level)."""
    paths = {getattr(r, "path", "") for r in app.routes}
    ws_paths = [p for p in paths if "/api/workspaces" in p]
    assert ws_paths, (
        "No /api/workspaces routes found in app.routes. "
        f"Available API routes: {sorted(p for p in paths if p.startswith('/api'))}"
    )


# ---------------------------------------------------------------------------
# P1-22: stop_all must be called on shutdown
# ---------------------------------------------------------------------------


def test_stop_all_method_exists() -> None:
    """AgentService must expose an async stop_all method."""
    assert hasattr(AgentService, "stop_all"), (
        "AgentService is missing stop_all"
    )
    assert inspect.iscoroutinefunction(AgentService.stop_all), (
        "AgentService.stop_all must be a coroutine function"
    )


async def test_lifespan_calls_stop_all_on_shutdown(tmp_path: pathlib.Path) -> None:
    """Lifespan teardown must await agent_svc.stop_all()."""
    os.environ["FLEET_DB_PATH"] = str(tmp_path / "wiring_stop.db")
    called: list[str] = []

    async def _spy_stop_all(self) -> None:
        called.append("stop_all")

    mock_app = MagicMock()
    mock_app.state = SimpleNamespace()

    try:
        # create=True lets the patch work even when stop_all doesn't exist yet
        with patch.object(AgentService, "stop_all", _spy_stop_all, create=True):
            async with lifespan(mock_app):
                pass  # lifespan teardown runs on context exit
    finally:
        os.environ.pop("FLEET_DB_PATH", None)

    assert "stop_all" in called, (
        "stop_all was not called during lifespan shutdown"
    )
