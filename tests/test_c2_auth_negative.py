"""C2: Auth negative regression tests.

Security-critical invariants not previously covered:
  1. require_token — non-Bearer scheme → 401
  2. require_token — empty Bearer value → 401
  3. require_agent_identity — no Authorization header → 401
  4. require_agent_identity — non-Bearer scheme → 401
  5. require_agent_identity — empty Bearer value → 401
  6. require_agent_identity — unknown token (no matching agent) → 401

Each test acts as a regression guard: if someone removes a check, the test
catches it immediately instead of silently letting the request through.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from fleet.api.auth import get_settings, require_agent_identity, require_token
from fleet.config import Settings
from fleet.db import DatabaseManager, init_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _make_token_app(settings: Settings) -> FastAPI:
    """Minimal FastAPI app protected by require_token."""
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/protected")
    async def protected(
        _: Annotated[None, Depends(require_token)],
    ) -> dict[str, str]:
        return {"ok": "true"}

    return app


# ---------------------------------------------------------------------------
# Fixtures for require_agent_identity tests
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "c2_auth.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def agent_identity_app(db: DatabaseManager) -> FastAPI:
    """Minimal FastAPI app protected by require_agent_identity."""
    from fleet.api.auth import set_auth_db

    set_auth_db(db)

    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: Settings(api_token="admin-tok")

    @app.get("/identity")
    async def identity_route(
        identity: Annotated[Any, Depends(require_agent_identity)],
    ) -> dict[str, str]:
        return {"agent_id": identity.agent_id or "admin"}

    return app


def _insert_agent(
    db: DatabaseManager,
    *,
    agent_id: str,
    plaintext_token: str,
    status: str = "idle",
) -> None:
    import sqlite3 as _sqlite3

    db_url = str(db.engine.url).replace("sqlite:///", "")
    conn = _sqlite3.connect(db_url)
    conn.execute(
        "INSERT INTO agents"
        " (id, name, scope, role, backend, model, status,"
        "  context_pct, cost_usd, created_at, updated_at, token_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)",
        (
            agent_id, agent_id, "scope", "worker", "mock", "mock",
            status, _now(), _now(), _sha256(plaintext_token),
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 1-2. require_token negative cases
# ---------------------------------------------------------------------------


class TestRequireTokenNegative:
    """require_token must reject non-standard auth schemes and empty tokens."""

    @pytest.mark.asyncio
    async def test_non_bearer_scheme_rejected(self) -> None:
        """Authorization: Basic abc must return 401 (not Bearer → rejected)."""
        settings = Settings(api_token="secret")
        app = _make_token_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/protected",
                headers={"Authorization": "Basic c2VjcmV0"},
            )
        assert resp.status_code == 401, (
            f"Non-Bearer scheme must be rejected with 401, got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_empty_bearer_value_rejected(self) -> None:
        """Authorization: Bearer <empty> must return 401."""
        settings = Settings(api_token="secret")
        app = _make_token_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/protected",
                headers={"Authorization": "Bearer "},
            )
        assert resp.status_code == 401, (
            f"Empty Bearer value must be rejected with 401, got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_digest_scheme_rejected(self) -> None:
        """Authorization: Digest ... must return 401."""
        settings = Settings(api_token="secret")
        app = _make_token_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/protected",
                headers={"Authorization": "Digest secret"},
            )
        assert resp.status_code == 401, (
            f"Digest scheme must be rejected with 401, got {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# 3-6. require_agent_identity negative cases
# ---------------------------------------------------------------------------


class TestRequireAgentIdentityNegative:
    """require_agent_identity must fail closed on all invalid auth inputs."""

    @pytest.mark.asyncio
    async def test_no_authorization_header_returns_401(
        self, agent_identity_app: FastAPI
    ) -> None:
        """No Authorization header → 401 Bearer token required."""
        async with AsyncClient(
            transport=ASGITransport(app=agent_identity_app), base_url="http://test"
        ) as client:
            resp = await client.get("/identity")
        assert resp.status_code == 401, (
            f"Missing auth header must return 401, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_non_bearer_scheme_returns_401(
        self, agent_identity_app: FastAPI
    ) -> None:
        """Authorization: Basic ... → 401 (not Bearer scheme)."""
        async with AsyncClient(
            transport=ASGITransport(app=agent_identity_app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/identity",
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )
        assert resp.status_code == 401, (
            f"Non-Bearer scheme must return 401, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_empty_bearer_value_returns_401(
        self, agent_identity_app: FastAPI
    ) -> None:
        """Authorization: Bearer <empty> → 401."""
        async with AsyncClient(
            transport=ASGITransport(app=agent_identity_app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/identity",
                headers={"Authorization": "Bearer "},
            )
        assert resp.status_code == 401, (
            f"Empty Bearer value must return 401, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_unknown_token_returns_401(
        self, agent_identity_app: FastAPI
    ) -> None:
        """Token that doesn't match any agent and isn't admin token → 401."""
        async with AsyncClient(
            transport=ASGITransport(app=agent_identity_app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/identity",
                headers={"Authorization": "Bearer completely-unknown-token-xyz"},
            )
        assert resp.status_code == 401, (
            f"Unknown token must return 401, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_bearer_keyword_only_returns_401(
        self, agent_identity_app: FastAPI
    ) -> None:
        """Authorization: Bearer (no space after) → 401."""
        async with AsyncClient(
            transport=ASGITransport(app=agent_identity_app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/identity",
                headers={"Authorization": "Bearer"},
            )
        assert resp.status_code == 401, (
            f"'Bearer' alone must return 401, got {resp.status_code}: {resp.text}"
        )
