"""Shared pytest fixtures for the Fleet test suite.

Provides:
  no_auth  — callable suitable for dependency_overrides[require_token]
             and dependency_overrides[require_agent_identity].
"""
from __future__ import annotations

import pytest

from fleet.api.auth import AgentIdentity


def no_auth() -> None:
    """Auth bypass for require_token: no-op, returns None."""


def admin_identity() -> AgentIdentity:
    """Auth bypass for require_agent_identity: returns an admin identity."""
    return AgentIdentity(agent_id=None, role=None, is_admin=True)


@pytest.fixture
def auth_bypass() -> dict:
    """Return a dict of {dependency: override} for skipping auth in tests.

    Usage::

        from fleet.api.auth import require_token, require_agent_identity
        app.dependency_overrides.update(auth_bypass)
    """
    from fleet.api.auth import require_agent_identity, require_token

    return {
        require_token: no_auth,
        require_agent_identity: admin_identity,
    }
