"""Tests for the 8 new pipeline-consolidation role-manifest entries (Task T5).

Follows the direct PolicyService/load_manifest unit-test pattern established
in tests/test_policy.py.
"""
from __future__ import annotations

import os

import pytest

from fleet.policy.rules import load_manifest
from fleet.policy.service import PolicyDenied, PolicyService


def _default_manifest_path() -> str:
    return os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "fleet",
        "manifests",
        "default.yaml",
    )


# One representative "allowed" tool per new role, taken from the role's grant.
ROLE_ALLOWED_TOOL = {
    "pm": "send_message",
    "ux": "send_message",
    "architect": "send_message",
    "junior-dev": "worker_wip",
    "senior-dev": "record_validation",
    "junior-qa": "record_validation",
    "senior-qa": "record_validation",
    "release": "list_agents",
}

NEW_ROLES = list(ROLE_ALLOWED_TOOL)


@pytest.mark.parametrize("role", NEW_ROLES)
def test_new_role_can_call_its_allowed_tool(role: str) -> None:
    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    # Must not raise.
    svc.check_tool_allowed(role=role, tool_name=ROLE_ALLOWED_TOOL[role])


@pytest.mark.parametrize("role", NEW_ROLES)
def test_new_role_cannot_call_spawn_worker(role: str) -> None:
    manifest = load_manifest(_default_manifest_path())
    svc = PolicyService(manifest)
    with pytest.raises(PolicyDenied) as exc_info:
        svc.check_tool_allowed(role=role, tool_name="spawn_worker")
    assert exc_info.value.role == role
    assert exc_info.value.tool_name == "spawn_worker"


def test_existing_four_roles_unchanged() -> None:
    """New role additions must not alter the 4 pre-existing roles (NFR3)."""
    manifest = load_manifest(_default_manifest_path())

    assert manifest.roles["orchestrator"].allowed_tools == [
        "spawn_worker",
        "send_message",
        "list_agents",
        "get_agent_logs",
        "stop_agent",
        "request_approval",
        "memory_write",
        "update_progress",
        "report_issue",
        "execute_merge",
    ]
    assert manifest.roles["coder"].allowed_tools == [
        "send_message",
        "get_agent_logs",
        "worker_wip",
        "check_conflict",
        "record_validation",
        "update_progress",
        "report_issue",
        "request_approval",
        "memory_write",
    ]
    assert manifest.roles["reviewer"].allowed_tools == [
        "record_validation",
        "worker_wip",
        "check_conflict",
        "get_agent_logs",
        "send_message",
        "update_progress",
        "report_issue",
        "memory_write",
    ]
    assert manifest.roles["observer"].allowed_tools == [
        "list_agents",
        "get_agent_logs",
    ]


def test_no_new_role_holds_privileged_tools() -> None:
    """Only orchestrator may hold spawn_worker / execute_merge / stop_agent."""
    manifest = load_manifest(_default_manifest_path())
    privileged = {"spawn_worker", "execute_merge", "stop_agent"}

    for role in NEW_ROLES:
        role_cfg = manifest.roles[role]
        overlap = privileged & set(role_cfg.allowed_tools)
        assert not overlap, f"role {role!r} must not hold {overlap}"
