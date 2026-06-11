"""API token authentication dependencies for Fleet FastAPI routes.

Two auth strategies:

  require_token          — validates FLEET_API_TOKEN (admin/human/dashboard/CLI).
  require_agent_identity — validates per-agent tokens for tool dispatch;
                           also accepts FLEET_API_TOKEN for admin impersonation.

Usage in a route:
    @router.post("/foo")
    async def foo(_: Annotated[None, Depends(require_token)]) -> ...:
        ...
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Query, Request
from sqlalchemy import text

from fleet.config import Settings

if TYPE_CHECKING:
    from fleet.db import DatabaseManager

# ---------------------------------------------------------------------------
# Module-level DB ref for require_agent_identity
# ---------------------------------------------------------------------------

_auth_db: DatabaseManager | None = None


def set_auth_db(db: DatabaseManager | None) -> None:
    """Wire the DatabaseManager for per-agent token lookups (called at startup)."""
    global _auth_db
    _auth_db = db


# ---------------------------------------------------------------------------
# AgentIdentity — result of require_agent_identity
# ---------------------------------------------------------------------------


@dataclass
class AgentIdentity:
    """Authenticated caller identity returned by require_agent_identity.

    is_admin=True: caller used FLEET_API_TOKEN (admin/impersonation path).
    is_admin=False: caller used their per-agent token; agent_id and role
    are set from the DB row.
    """

    agent_id: str | None  # None for admin (identity taken from body)
    role: str | None      # None for admin (role looked up per-request)
    is_admin: bool = False


# ---------------------------------------------------------------------------
# Settings factory
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Per-agent identity dependency
# ---------------------------------------------------------------------------


async def require_agent_identity(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AgentIdentity:
    """Authenticate the caller as either admin or agent.

    Per-agent path: ``Authorization: Bearer <agent_token>`` where the token's
    SHA-256 hash matches ``agents.token_hash``.  Returns an ``AgentIdentity``
    with ``agent_id`` and ``role`` populated.

    Admin path: bearer token matches ``FLEET_API_TOKEN``.  Returns
    ``AgentIdentity(is_admin=True)``.  The caller's ``agent_id`` is taken
    from the request body (impersonation); the caller must supply it explicitly.

    Raises 401 when:
    - No Bearer token is present.
    - Token doesn't match any agent hash AND doesn't match FLEET_API_TOKEN.
    - Matched agent has status ``archived`` (token revoked).
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = auth_header[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    configured = settings.api_token.strip()

    # Admin path: FLEET_API_TOKEN takes precedence.
    if configured and hmac.compare_digest(token, configured):
        return AgentIdentity(agent_id=None, role=None, is_admin=True)

    # Per-agent path: hash lookup.
    db = _auth_db
    if db is None:
        raise HTTPException(status_code=503, detail="Auth DB not configured")

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT id, role, status FROM agents WHERE token_hash = :h"),
            {"h": token_hash},
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    agent_id_str = str(row[0])
    role_str = str(row[1])
    status_str = str(row[2])

    if status_str == "archived":
        raise HTTPException(status_code=401, detail="Unauthorized")

    return AgentIdentity(agent_id=agent_id_str, role=role_str, is_admin=False)
