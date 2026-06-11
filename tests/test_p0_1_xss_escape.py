"""P0-1: XSS escape in dashboard error rendering.

The /dashboard/approvals/{id}/decide endpoint renders exception messages
directly into HTMLResponse f-strings.  Unescaped output is classic reflected
XSS — attacker-influenced text (e.g. approval comments) that flows into
exception messages would execute in the operator's browser.
"""
from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from httpx import ASGITransport, AsyncClient

from fleet.api.auth import require_token
from fleet.dashboard.router import (
    router as dashboard_router,
    set_approval_service,
    set_db,
    set_templates,
)


_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "fleet" / "templates"
_XSS_PAYLOAD = "<script>alert(1)</script>"


def _make_dashboard_app(approval_svc_mock: object) -> FastAPI:
    """Minimal FastAPI app with dashboard router; globals set synchronously."""
    # Set module-level globals directly (ASGITransport does not run lifespan).
    set_db(None)  # type: ignore[arg-type]
    set_templates(Jinja2Templates(directory=str(_TEMPLATES_DIR)))
    set_approval_service(approval_svc_mock)  # type: ignore[arg-type]

    app = FastAPI()
    app.dependency_overrides[require_token] = lambda: None
    app.include_router(dashboard_router)
    return app


@pytest.mark.asyncio
async def test_dashboard_error_escapes_html_in_decide_valueerror() -> None:
    """ValueError from decide() containing HTML must be escaped in 400 response."""
    svc = AsyncMock()
    svc.decide = AsyncMock(
        side_effect=ValueError(f"Decision failed: {_XSS_PAYLOAD}")
    )

    app = _make_dashboard_app(svc)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/dashboard/approvals/fake-id/decide",
            data={"decision": "approve", "comment": ""},
        )

    assert resp.status_code == 400
    body = resp.text
    assert "&lt;script&gt;" in body, "HTML not escaped in error response"
    assert "<script>" not in body, "Raw <script> tag present — XSS vulnerability"


@pytest.mark.asyncio
async def test_dashboard_error_escapes_html_in_decide_db_error() -> None:
    """SQLAlchemyError message with HTML payload must be escaped in 500 response."""
    from sqlalchemy.exc import SQLAlchemyError

    svc = AsyncMock()
    svc.decide = AsyncMock(
        side_effect=SQLAlchemyError(f"DB error: {_XSS_PAYLOAD}")
    )

    app = _make_dashboard_app(svc)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/dashboard/approvals/fake-id/decide",
            data={"decision": "approve", "comment": ""},
        )

    assert resp.status_code == 500
    body = resp.text
    assert "&lt;script&gt;" in body, "HTML not escaped in error response"
    assert "<script>" not in body, "Raw <script> tag present — XSS vulnerability"
