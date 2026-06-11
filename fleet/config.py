"""Fleet configuration — reads from environment variables."""

from __future__ import annotations

import ipaddress
import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(Exception):
    """Raised when Fleet's startup configuration is invalid."""


class Settings(BaseSettings):
    """Application settings, sourced from environment variables."""

    model_config = SettingsConfigDict(env_prefix="FLEET_", case_sensitive=False)

    # env var names: FLEET_API_TOKEN, FLEET_HOST, FLEET_PORT, FLEET_DB_PATH,
    # FLEET_SECRET_PATTERNS, FLEET_COMPACTION_THRESHOLD
    api_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "fleet.db"
    secret_patterns: list[str] = ["FLEET_API_TOKEN"]
    compaction_threshold: int = 80_000
    gate_require_reviewer: bool = True
    scope_budget_hard_usd: float | None = None

    def is_local_bind(self) -> bool:
        """Return True iff the host is a loopback address."""
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False

    def validate_for_startup(self) -> None:
        """Raise ConfigurationError if the configuration is unsafe to start with.

        Rules:
        - A non-local bind with no API token means the server is reachable from
          the network with no authentication, which is forbidden.
        - WEB_CONCURRENCY > 1 means multiple processes would share in-memory
          state (e.g. MergeLock) — Fleet requires a single process.

        Note: When ``FLEET_API_TOKEN`` is empty and binding to loopback,
        authentication is bypassed for loopback clients (local dev mode).
        """
        if not self.api_token.strip() and not self.is_local_bind():
            raise ConfigurationError(
                "FLEET_API_TOKEN must be set when binding to a non-loopback address "
                f"(current host: {self.host!r}). "
                "Set the token or restrict the bind address to 127.0.0.1 / ::1."
            )

        web_concurrency = int(os.environ.get("WEB_CONCURRENCY", "1"))
        if web_concurrency > 1:
            raise ConfigurationError(
                f"Fleet requires a single process (WEB_CONCURRENCY=1), "
                f"got WEB_CONCURRENCY={web_concurrency}. "
                "The in-memory merge lock is not safe for multi-process deployments. "
                "Run with a single worker or migrate MergeLock to SQLite."
            )
