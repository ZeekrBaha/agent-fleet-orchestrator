"""PolicyService — fail-closed tool-call policy gating (ADR-005).

Every tool call must pass check_tool_allowed() before execution.
Unknown roles and unknown tools are always denied; there is no fallback allow.
"""
from __future__ import annotations

import fnmatch
import logging

from fleet.policy.rules import ManifestConfig, RoleConfig

logger = logging.getLogger(__name__)


class PolicyDenied(Exception):
    """Raised when a tool call is denied by policy."""

    def __init__(self, reason: str, tool_name: str, role: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tool_name = tool_name
        self.role = role


class PolicyService:
    """Gate every tool call against the loaded role manifest (fail-closed)."""

    def __init__(self, manifest: ManifestConfig) -> None:
        self._manifest = manifest

    def check_tool_allowed(self, role: str, tool_name: str) -> None:
        """Raise PolicyDenied if role is unknown or tool is not in role's allowlist.

        ADR-005: unknown role OR unknown tool → always deny, never silently allow.
        """
        role_cfg = self._manifest.roles.get(role)
        if role_cfg is None:
            reason = f"Unknown role {role!r} — access denied (fail-closed)"
            logger.warning(
                "policy_denied role=%r tool=%r reason=%s", role, tool_name, reason
            )
            raise PolicyDenied(reason=reason, tool_name=tool_name, role=role)

        if tool_name not in role_cfg.allowed_tools:
            reason = f"Role {role!r} is not permitted to call tool {tool_name!r}"
            logger.warning(
                "policy_denied role=%r tool=%r reason=%s", role, tool_name, reason
            )
            raise PolicyDenied(reason=reason, tool_name=tool_name, role=role)

    def check_secret_path(self, path: str) -> None:
        """Raise PolicyDenied if path matches any secret_paths glob pattern.

        Uses fnmatch.fnmatch for glob matching. The path is normalised to
        use forward slashes and matched case-insensitively on all platforms
        (consistent with macOS/Windows behaviour described in ADR-005).
        """
        normalised = path.replace("\\", "/").lower()

        for pattern in self._manifest.secret_paths:
            # Strip the leading **/ prefix so bare filenames also match;
            # e.g. "**/.env" should match ".env" at any depth.
            bare_pattern = pattern.lstrip("*").lstrip("/").lower()
            if fnmatch.fnmatch(normalised, pattern.lower()):
                reason = f"Path {path!r} matches secret pattern {pattern!r}"
                logger.warning("policy_denied path=%r pattern=%r", path, pattern)
                raise PolicyDenied(
                    reason=reason, tool_name="<path_check>", role="<n/a>"
                )
            # Match the bare filename/last segment against the bare pattern
            # so ".env" matches "**/.env".
            if fnmatch.fnmatch(normalised, bare_pattern):
                reason = f"Path {path!r} matches secret pattern {pattern!r}"
                logger.warning("policy_denied path=%r pattern=%r", path, pattern)
                raise PolicyDenied(
                    reason=reason, tool_name="<path_check>", role="<n/a>"
                )
            # Match any path suffix; "a/b/.ssh/id_rsa" should match "**/.ssh/**"
            for suffix in _path_suffixes(normalised):
                if fnmatch.fnmatch(suffix, bare_pattern):
                    reason = f"Path {path!r} matches secret pattern {pattern!r}"
                    logger.warning("policy_denied path=%r pattern=%r", path, pattern)
                    raise PolicyDenied(
                        reason=reason, tool_name="<path_check>", role="<n/a>"
                    )

    def check_spawn_rate(
        self,
        scope: str,
        role: str,
        current_live_workers: int,
        spawns_last_minute: int,
    ) -> None:
        """Raise PolicyDenied if spawn rate limits are exceeded.

        Checks:
        - current_live_workers >= max_live_workers
        - spawns_last_minute >= max_spawns_per_minute
        """
        role_cfg = self.get_role_config(role)
        limits = role_cfg.spawn_rate

        if current_live_workers >= limits.max_live_workers:
            reason = (
                f"Live worker limit reached: {current_live_workers} >= "
                f"{limits.max_live_workers} (scope={scope!r}, role={role!r})"
            )
            logger.warning(
                "policy_denied spawn_rate scope=%r role=%r reason=%s",
                scope,
                role,
                reason,
            )
            raise PolicyDenied(reason=reason, tool_name="spawn_worker", role=role)

        if spawns_last_minute >= limits.max_spawns_per_minute:
            reason = (
                f"Spawn-per-minute limit reached: {spawns_last_minute} >= "
                f"{limits.max_spawns_per_minute} (scope={scope!r}, role={role!r})"
            )
            logger.warning(
                "policy_denied spawn_rate scope=%r role=%r reason=%s",
                scope,
                role,
                reason,
            )
            raise PolicyDenied(reason=reason, tool_name="spawn_worker", role=role)

    def get_role_config(self, role: str) -> RoleConfig:
        """Return role config. Raise PolicyDenied if role unknown (fail-closed)."""
        cfg = self._manifest.roles.get(role)
        if cfg is None:
            reason = f"Unknown role {role!r} — access denied (fail-closed)"
            logger.warning("policy_denied get_role_config role=%r", role)
            raise PolicyDenied(reason=reason, tool_name="<role_lookup>", role=role)
        return cfg


def _path_suffixes(path: str) -> list[str]:
    """Return all path suffixes (sub-paths from each depth).

    E.g. "a/b/.ssh/id_rsa" → all suffixes from root down to bare filename.
    """
    parts = path.split("/")
    return ["/".join(parts[i:]) for i in range(len(parts))]
