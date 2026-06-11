"""Canonical timestamp helper for Fleet."""
from __future__ import annotations

from datetime import UTC, datetime


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with +00:00 suffix.

    All Fleet services use this single function so timestamps are consistent
    and lexicographically comparable (the +00:00 form sorts correctly).
    """
    return datetime.now(UTC).isoformat()
