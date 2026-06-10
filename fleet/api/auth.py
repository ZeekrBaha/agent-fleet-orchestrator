"""API token authentication dependency for Fleet FastAPI routes.

Usage in a route:
    @router.post("/foo")
    async def foo(_: Annotated[None, Depends(require_token)]) -> ...:
        ...
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from fleet.config import Settings


def get_settings() -> Settings:
    """Return a fresh Settings instance (reads env-vars on each call)."""
    return Settings()


async def require_token(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Verify the ``Authorization: Bearer <token>`` header.

    Raises HTTP 401 if:
    - The header is absent.
    - The scheme is not ``Bearer``.
    - The token does not match ``FLEET_API_TOKEN``.
    - ``FLEET_API_TOKEN`` is empty (no token configured → always reject).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = auth_header[len("Bearer "):].strip()
    configured = settings.api_token.strip()

    # An empty configured token means auth is not set up — always reject.
    if not configured or token != configured:
        raise HTTPException(status_code=401, detail="Unauthorized")
