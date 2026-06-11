"""P1-1: Startup guard against multi-worker deployments.

Fleet's MergeLock is an in-memory asyncio.Lock — correct for one process,
silently broken if two uvicorn workers run on the same repo (concurrent merges
can interleave).

Minimal fix: refuse to start when WEB_CONCURRENCY > 1 (Heroku/Render/Railway
convention).  This removes the silent-corruption mode and makes the
single-process invariant explicit.
"""
from __future__ import annotations

import pytest

from fleet.config import ConfigurationError, Settings


def test_startup_rejects_web_concurrency_greater_than_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_for_startup must raise ConfigurationError when WEB_CONCURRENCY > 1."""
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setenv("FLEET_API_TOKEN", "tok")  # satisfy token requirement

    s = Settings(api_token="tok", host="127.0.0.1")
    with pytest.raises(ConfigurationError, match=r"single.process|WEB_CONCURRENCY"):
        s.validate_for_startup()


def test_startup_allows_web_concurrency_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WEB_CONCURRENCY=1 must not raise."""
    monkeypatch.setenv("WEB_CONCURRENCY", "1")

    s = Settings(api_token="tok", host="127.0.0.1")
    s.validate_for_startup()  # must not raise


def test_startup_allows_missing_web_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If WEB_CONCURRENCY is absent, default is 1 — must not raise."""
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    s = Settings(api_token="tok", host="127.0.0.1")
    s.validate_for_startup()  # must not raise
