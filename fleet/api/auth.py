"""API token authentication dependency for Fleet FastAPI routes.

Usage in a route:
    @router.post("/foo")
    async def foo(_: Annotated[None, Depends(require_token)]) -> ...:
        ...
"""

from __future__ import annotations

import functools
import hmac
import ipaddress
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request

from fleet.config import Settings


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance (reads env-vars once per process)."""
    return Settings()


async def require_token(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    token_param: str | None = Query(default=None, alias="token"),
) -> None:
    """Verify request authentication.

    Accepts credentials in two forms (checked in order):

    1. ``Authorization: Bearer <token>`` header — for standard API clients.
    2. ``?token=<token>`` query parameter — for ``EventSource`` clients that
       cannot send custom headers.

    Loopback bypass: when ``FLEET_API_TOKEN`` is empty *and* the request
    originates from a loopback address (127.0.0.1 / ::1), authentication is
    bypassed entirely (local dev mode with no token configured).

    Raises HTTP 401 if:
    - No valid credential is supplied.
    - The provided token does not match ``FLEET_API_TOKEN``.
    - ``FLEET_API_TOKEN`` is empty and the client is not on loopback.
    """
    configured = settings.api_token.strip()

    # Loopback bypass: empty token + loopback client → allow (local dev mode).
    if not configured:
        client_host = request.client.host if request.client else ""
        try:
            if ipaddress.ip_address(client_host).is_loopback:
                return
        except ValueError:
            pass
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Try Authorization: Bearer header first (standard API clients).
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()
        if hmac.compare_digest(token, configured):
            return

    # Fall back to ?token= query param (for EventSource which can't send headers).
    if token_param is not None and hmac.compare_digest(token_param, configured):
        return

    raise HTTPException(status_code=401, detail="Unauthorized")
