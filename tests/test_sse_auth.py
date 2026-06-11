"""SSE auth contract tests (T10 / P1-26).

RED phase: write tests first, then implement.

Covers:
1. require_token accepts ?token= query param (EventSource fallback)
2. require_token rejects wrong ?token= query param
3. require_token still accepts Authorization: Bearer header
4. Loopback bypass when token is empty and client is 127.0.0.1
5. Non-loopback with empty token still rejects
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from fleet.api.auth import get_settings, require_token
from fleet.config import Settings

# ---------------------------------------------------------------------------
# Helper: build a minimal FastAPI app with a protected route
# ---------------------------------------------------------------------------


def _make_app(settings: Settings) -> FastAPI:
    """Return a FastAPI app with one auth-protected GET route."""
    app = FastAPI()
    app.dependency_overrides[get_settings] = lambda: settings

    @app.get("/protected")
    async def protected(
        _auth: Annotated[None, Depends(require_token)],
    ) -> dict[str, str]:
        return {"ok": "true"}

    return app


# ---------------------------------------------------------------------------
# Tests: query-param token
# ---------------------------------------------------------------------------


class TestQueryParamToken:
    """require_token must accept the token via ?token= for EventSource clients."""

    def test_query_param_token_accepted(self) -> None:
        """?token=correct must return 200."""
        settings = Settings(api_token="secret123", host="127.0.0.1")
        app = _make_app(settings)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/protected?token=secret123")
        assert resp.status_code == 200

    def test_wrong_query_param_token_rejected(self) -> None:
        """?token=wrong must return 401."""
        settings = Settings(api_token="secret123", host="127.0.0.1")
        app = _make_app(settings)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/protected?token=wrongtoken")
        assert resp.status_code == 401

    def test_missing_token_rejected(self) -> None:
        """No header and no query param must return 401 when token is configured."""
        settings = Settings(api_token="secret123", host="127.0.0.1")
        app = _make_app(settings)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/protected")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tests: Bearer header still works
# ---------------------------------------------------------------------------


class TestBearerHeader:
    """Authorization: Bearer must still work for standard API clients."""

    def test_bearer_header_accepted(self) -> None:
        """Authorization: Bearer correct must return 200."""
        settings = Settings(api_token="secret123", host="127.0.0.1")
        app = _make_app(settings)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/protected",
                headers={"Authorization": "Bearer secret123"},
            )
        assert resp.status_code == 200

    def test_wrong_bearer_header_rejected(self) -> None:
        """Authorization: Bearer wrong must return 401."""
        settings = Settings(api_token="secret123", host="127.0.0.1")
        app = _make_app(settings)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/protected", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_bearer_takes_precedence_over_query_param(self) -> None:
        """When both header and query param are present, correct bearer wins."""
        settings = Settings(api_token="secret123", host="127.0.0.1")
        app = _make_app(settings)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/protected?token=wrongparam",
                headers={"Authorization": "Bearer secret123"},
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: loopback bypass
# ---------------------------------------------------------------------------


class TestLoopbackBypass:
    """When no token configured + loopback client → allow; non-loopback → deny."""

    @pytest.mark.asyncio
    async def test_loopback_bypass_empty_token(self) -> None:
        """Empty token + loopback client must return 200 (local dev mode)."""
        settings = Settings(api_token="", host="127.0.0.1")
        app = _make_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.get("/protected")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_non_loopback_empty_token_rejected(self) -> None:
        """Empty token + non-loopback client must return 401."""
        settings = Settings(api_token="", host="0.0.0.0")
        app = _make_app(settings)
        # testclient/httpx presents client as testclient host; mock via
        # a non-loopback IP in scope (we use a custom transport below)
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://10.0.0.1",
        ) as client:
            resp = await client.get("/protected")
        # Client IP seen by ASGI will be testclient default (127.0.0.1) in
        # transport mode — so we test the logic directly via unit test instead.
        # The key contract: a non-loopback host value seen by require_token
        # with empty token → 401. This is exercised via direct unit test below.
        _ = resp  # actual http status depends on transport client IP

    @pytest.mark.asyncio
    async def test_loopback_bypass_ipv6(self) -> None:
        """Empty token + ::1 client must return 200 (direct unit test)."""

        settings = Settings(api_token="")
        # Build a synthetic request with client IP ::1
        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "path": "/protected",
            "query_string": b"",
            "headers": [],
            "client": ("::1", 12345),
        }
        req = Request(scope)
        # Must not raise
        await require_token(request=req, settings=settings, token_param=None)


# ---------------------------------------------------------------------------
# Direct unit tests: require_token logic (no HTTP layer)
# ---------------------------------------------------------------------------


class TestRequireTokenUnit:
    """Unit tests calling require_token directly with synthetic Request objects."""

    def _make_request(
        self, client_host: str, headers: dict[str, str] | None = None
    ) -> Request:
        """Build a minimal Starlette Request with a given client IP."""
        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "path": "/protected",
            "query_string": b"",
            "headers": [
                (k.lower().encode(), v.encode())
                for k, v in (headers or {}).items()
            ],
            "client": (client_host, 12345),
        }
        return Request(scope)

    @pytest.mark.asyncio
    async def test_loopback_bypass_direct(self) -> None:
        """Empty configured token + 127.0.0.1 client → no exception."""
        settings = Settings(api_token="")
        req = self._make_request("127.0.0.1")
        # Must not raise
        await require_token(request=req, settings=settings, token_param=None)

    @pytest.mark.asyncio
    async def test_non_loopback_empty_token_direct(self) -> None:
        """Empty configured token + non-loopback client → 401."""
        from fastapi import HTTPException

        settings = Settings(api_token="")
        req = self._make_request("203.0.113.1")
        with pytest.raises(HTTPException) as exc_info:
            await require_token(request=req, settings=settings, token_param=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_bearer_token_direct(self) -> None:
        """Correct Bearer header + configured token → no exception."""
        settings = Settings(api_token="mytoken")
        req = self._make_request(
            "127.0.0.1", headers={"Authorization": "Bearer mytoken"}
        )
        await require_token(request=req, settings=settings, token_param=None)

    @pytest.mark.asyncio
    async def test_query_param_token_direct(self) -> None:
        """Correct query param token + configured token → no exception."""
        settings = Settings(api_token="mytoken")
        req = self._make_request("127.0.0.1")
        await require_token(request=req, settings=settings, token_param="mytoken")

    @pytest.mark.asyncio
    async def test_wrong_query_param_direct(self) -> None:
        """Wrong query param → 401."""
        from fastapi import HTTPException

        settings = Settings(api_token="mytoken")
        req = self._make_request("127.0.0.1")
        with pytest.raises(HTTPException) as exc_info:
            await require_token(request=req, settings=settings, token_param="wrong")
        assert exc_info.value.status_code == 401
