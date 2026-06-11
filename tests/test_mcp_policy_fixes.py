"""Tests for T6: MCP toolserver schema fixes + policy wiring.

P1-7: MCP toolserver schema drift
  - spawn_worker MCP tool must expose task_id parameter.
  - execute_merge must be registered as an MCP tool in the toolserver.

P1-8: check_secret_path wiring
  - _handle_spawn_worker must block 403 for owned_paths containing a secret path.
  - _handle_spawn_worker must succeed when paths are clean.
"""

from __future__ import annotations

import inspect
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

_DEFAULT_MANIFEST_PATH = os.path.join(
    os.path.dirname(__file__), "..", "fleet", "manifests", "default.yaml"
)

# ---------------------------------------------------------------------------
# P1-7: MCP toolserver schema tests
# ---------------------------------------------------------------------------


def test_spawn_worker_mcp_has_task_id_param() -> None:
    """spawn_worker MCP tool must expose task_id parameter."""
    from fleet.toolserver import main as toolserver_main  # noqa: PLC0415

    sig = inspect.signature(toolserver_main.spawn_worker)
    assert "task_id" in sig.parameters, (
        "spawn_worker MCP tool is missing 'task_id' parameter"
    )
    # task_id must be optional — workers spawned without a task get no worktree.
    param = sig.parameters["task_id"]
    assert param.default is None, "task_id must default to None"


def test_execute_merge_mcp_is_registered() -> None:
    """execute_merge must be registered as an MCP tool in the toolserver."""
    from fleet.toolserver import main as toolserver_main  # noqa: PLC0415

    assert hasattr(toolserver_main, "execute_merge"), (
        "execute_merge function not found in fleet.toolserver.main — "
        "it is in _TOOL_REGISTRY but has no @mcp.tool() registration"
    )
    assert inspect.iscoroutinefunction(toolserver_main.execute_merge), (
        "execute_merge must be an async function"
    )


# ---------------------------------------------------------------------------
# P1-8: check_secret_path wiring tests
# ---------------------------------------------------------------------------


def _make_policy_svc() -> Any:
    """Return a real PolicyService backed by the default manifest."""
    from fleet.policy.rules import load_manifest  # noqa: PLC0415
    from fleet.policy.service import PolicyService  # noqa: PLC0415

    return PolicyService(load_manifest(_DEFAULT_MANIFEST_PATH))


def _make_spawn_svcs(calling_role: str = "orchestrator") -> dict[str, Any]:
    """Build the minimal svcs dict needed by _handle_spawn_worker."""
    calling_agent = MagicMock()
    calling_agent.role = calling_role

    mock_record = MagicMock()
    mock_record.id = "new-agent-id"
    mock_record.name = "worker-1"
    mock_record.status = "idle"

    mock_agent_svc = AsyncMock()
    mock_agent_svc.list_agents.return_value = []
    mock_agent_svc.get_agent.return_value = calling_agent
    mock_agent_svc.create_agent.return_value = mock_record

    mock_event_svc = AsyncMock()
    mock_event_svc.query.return_value = []
    mock_event_svc.append.return_value = None

    return {
        "agent_svc": mock_agent_svc,
        "event_svc": mock_event_svc,
        "_policy_svc": _make_policy_svc(),
        "_calling_agent": calling_agent,
        # worktree_svc intentionally omitted — inp.task_id is None so skipped
    }


@pytest.mark.asyncio
async def test_handle_spawn_worker_blocks_secret_path() -> None:
    """_handle_spawn_worker must raise HTTP 403 when owned_paths contains a secret."""
    from fleet.api.tool_handlers import _handle_spawn_worker  # noqa: PLC0415
    from fleet.api.tool_schemas import SpawnWorkerInput  # noqa: PLC0415

    inp = SpawnWorkerInput(
        agent_id="orch-1",
        scope="scope-1",
        name="worker-secret",
        role="worker",
        task_description="test",
        owned_paths=["config/.env"],  # matches **/.env secret pattern
    )
    svcs = _make_spawn_svcs()

    with pytest.raises(HTTPException) as exc_info:
        await _handle_spawn_worker(inp, svcs)

    assert exc_info.value.status_code == 403
    assert ".env" in exc_info.value.detail


@pytest.mark.asyncio
async def test_handle_spawn_worker_allows_clean_path() -> None:
    """_handle_spawn_worker must succeed when owned_paths contains only safe paths."""
    from fleet.api.tool_handlers import _handle_spawn_worker  # noqa: PLC0415
    from fleet.api.tool_schemas import SpawnWorkerInput  # noqa: PLC0415

    inp = SpawnWorkerInput(
        agent_id="orch-1",
        scope="scope-1",
        name="worker-clean",
        role="worker",
        task_description="test",
        owned_paths=["src/main.py", "tests/test_foo.py"],
    )
    svcs = _make_spawn_svcs()

    result = await _handle_spawn_worker(inp, svcs)

    assert result["agent_id"] == "new-agent-id"
    assert result["status"] == "idle"
