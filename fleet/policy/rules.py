"""Policy rules — Pydantic models and YAML manifest loader.

Defines the data structures for role-based tool policies (ADR-005).
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError


class SpawnRateConfig(BaseModel):
    max_live_workers: int = 10
    max_spawns_per_minute: int = 5


class RoleConfig(BaseModel):
    allowed_tools: list[str]
    spawn_rate: SpawnRateConfig = SpawnRateConfig()


class ShellRules(BaseModel):
    timeout_s: int = 30
    env_allowlist: list[str] = []


class ManifestConfig(BaseModel):
    version: str
    roles: dict[str, RoleConfig]
    secret_paths: list[str] = []
    shell_rules: ShellRules = ShellRules()


def load_manifest(path: str | Path) -> ManifestConfig:
    """Load and validate a YAML manifest file.

    Raises:
        FileNotFoundError: if path does not exist.
        ValueError: if YAML is malformed or fails Pydantic schema validation.
    """
    path = Path(path)
    with path.open("r") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(f"Manifest at {path} is not a YAML mapping")

    try:
        return ManifestConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Manifest schema error in {path}: {exc}") from exc


def load_default_manifest() -> ManifestConfig:
    """Load fleet/manifests/default.yaml relative to this package."""
    here = Path(__file__).parent  # fleet/policy/
    manifest_path = here.parent / "manifests" / "default.yaml"
    return load_manifest(manifest_path)
