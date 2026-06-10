"""MCP stdio server entrypoint for the Fleet tool server (Task 4.1).

This is a *separate process* — it reads tool calls from stdin (MCP stdio
protocol), validates each call against Pydantic input schemas, then relays
the validated payload to the Fleet API over localhost HTTP.

The process is untrusted: policy is enforced API-side.  The relay attaches
FLEET_API_TOKEN from the environment to every outbound request.

Environment variables (all optional except FLEET_API_TOKEN for non-dev):
    FLEET_API_TOKEN  — Bearer token sent to the Fleet API
    FLEET_BASE_URL   — Fleet API base URL (default: http://127.0.0.1:8000)

Usage:
    python -m fleet.toolserver.main       # stdio transport (default)
    # or via pyproject entry-point if configured
"""

from __future__ import annotations

import asyncio
import os

from mcp.server.fastmcp import FastMCP

from fleet.api.tool_schemas import (
    CheckConflictInput,
    GetAgentLogsInput,
    ListAgentsInput,
    MemoryWriteInput,
    RecordValidationInput,
    ReportIssueInput,
    RequestApprovalInput,
    SendMessageInput,
    SpawnWorkerInput,
    StopAgentInput,
    UpdateProgressInput,
    WorkerWipInput,
)
from fleet.toolserver.relay import FleetRelay

# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------

mcp = FastMCP("fleet-toolserver")


def _get_relay() -> FleetRelay:
    """Construct (or return cached) relay from env vars."""
    token = os.environ.get("FLEET_API_TOKEN", "")
    base_url = os.environ.get("FLEET_BASE_URL", "http://127.0.0.1:8000")
    return FleetRelay(base_url=base_url, token=token)


# ---------------------------------------------------------------------------
# Tool registrations — one per tool in the registry
# Each tool: validate args → relay.call() → return result dict
# ---------------------------------------------------------------------------


@mcp.tool()
async def spawn_worker(
    agent_id: str,
    scope: str,
    name: str,
    role: str,
    task_description: str,
    model: str = "claude-sonnet-4-6",
    repository_id: str | None = None,
    owned_paths: list[str] | None = None,
    budget_soft_usd: float | None = None,
    budget_hard_usd: float | None = None,
) -> dict[str, object]:
    """Spawn a new worker agent under this orchestrator."""
    inp = SpawnWorkerInput(
        agent_id=agent_id,
        scope=scope,
        name=name,
        role=role,
        task_description=task_description,
        model=model,
        repository_id=repository_id,
        owned_paths=owned_paths or [],
        budget_soft_usd=budget_soft_usd,
        budget_hard_usd=budget_hard_usd,
    )
    relay = _get_relay()
    return await relay.call("spawn_worker", agent_id, scope, inp.model_dump())


@mcp.tool()
async def send_message(
    agent_id: str,
    scope: str,
    target_agent_id: str,
    message: str,
) -> dict[str, object]:
    """Send a message to another agent's inbox."""
    inp = SendMessageInput(
        agent_id=agent_id,
        scope=scope,
        target_agent_id=target_agent_id,
        message=message,
    )
    relay = _get_relay()
    return await relay.call("send_message", agent_id, scope, inp.model_dump())


@mcp.tool()
async def list_agents(
    agent_id: str,
    scope: str,
) -> dict[str, object]:
    """List all active agents in scope."""
    inp = ListAgentsInput(agent_id=agent_id, scope=scope)
    relay = _get_relay()
    return await relay.call("list_agents", agent_id, scope, inp.model_dump())


@mcp.tool()
async def get_agent_logs(
    agent_id: str,
    scope: str,
    target_agent_id: str,
    limit: int = 50,
) -> dict[str, object]:
    """Retrieve recent event log entries for a specific agent."""
    inp = GetAgentLogsInput(
        agent_id=agent_id,
        scope=scope,
        target_agent_id=target_agent_id,
        limit=limit,
    )
    relay = _get_relay()
    return await relay.call("get_agent_logs", agent_id, scope, inp.model_dump())


@mcp.tool()
async def stop_agent(
    agent_id: str,
    scope: str,
    target_agent_id: str,
    reason: str = "",
) -> dict[str, object]:
    """Stop and archive a worker agent."""
    inp = StopAgentInput(
        agent_id=agent_id,
        scope=scope,
        target_agent_id=target_agent_id,
        reason=reason,
    )
    relay = _get_relay()
    return await relay.call("stop_agent", agent_id, scope, inp.model_dump())


@mcp.tool()
async def worker_wip(
    agent_id: str,
    scope: str,
    target_agent_id: str,
) -> dict[str, object]:
    """Get the work-in-progress status of a worker agent's worktree."""
    inp = WorkerWipInput(
        agent_id=agent_id,
        scope=scope,
        target_agent_id=target_agent_id,
    )
    relay = _get_relay()
    return await relay.call("worker_wip", agent_id, scope, inp.model_dump())


@mcp.tool()
async def check_conflict(
    agent_id: str,
    scope: str,
    worktree_id: str,
    target_branch: str = "main",
) -> dict[str, object]:
    """Check whether a worktree would conflict with the target branch on merge."""
    inp = CheckConflictInput(
        agent_id=agent_id,
        scope=scope,
        worktree_id=worktree_id,
        target_branch=target_branch,
    )
    relay = _get_relay()
    return await relay.call("check_conflict", agent_id, scope, inp.model_dump())


@mcp.tool()
async def record_validation(
    agent_id: str,
    scope: str,
    task_id: str,
    command: str,
    exit_code: int,
    summary: str,
    skipped: str | None = None,
    residual_risk: str | None = None,
) -> dict[str, object]:
    """Record a validation evidence entry for a task."""
    inp = RecordValidationInput(
        agent_id=agent_id,
        scope=scope,
        task_id=task_id,
        command=command,
        exit_code=exit_code,
        summary=summary,
        skipped=skipped,
        residual_risk=residual_risk,
    )
    relay = _get_relay()
    return await relay.call("record_validation", agent_id, scope, inp.model_dump())


@mcp.tool()
async def report_issue(
    agent_id: str,
    scope: str,
    title: str,
    description: str,
    severity: str = "info",
) -> dict[str, object]:
    """Report an issue or anomaly discovered during task execution."""
    inp = ReportIssueInput(
        agent_id=agent_id,
        scope=scope,
        title=title,
        description=description,
        severity=severity,
    )
    relay = _get_relay()
    return await relay.call("report_issue", agent_id, scope, inp.model_dump())


@mcp.tool()
async def update_progress(
    agent_id: str,
    scope: str,
    message: str,
    percent: int | None = None,
) -> dict[str, object]:
    """Report task progress to the orchestrator."""
    inp = UpdateProgressInput(
        agent_id=agent_id,
        scope=scope,
        message=message,
        percent=percent,
    )
    relay = _get_relay()
    return await relay.call("update_progress", agent_id, scope, inp.model_dump())


@mcp.tool()
async def request_approval(
    agent_id: str,
    scope: str,
    operation: str,
    rationale: str,
    risk: str,
) -> dict[str, object]:
    """Request human approval for a risky operation before proceeding."""
    inp = RequestApprovalInput(
        agent_id=agent_id,
        scope=scope,
        operation=operation,
        rationale=rationale,
        risk=risk,
    )
    relay = _get_relay()
    return await relay.call("request_approval", agent_id, scope, inp.model_dump())


@mcp.tool()
async def memory_write(
    agent_id: str,
    scope: str,
    kind: str,
    title: str,
    body: str,
) -> dict[str, object]:
    """Persist a memory entry for future retrieval by agents in this scope."""
    inp = MemoryWriteInput(
        agent_id=agent_id,
        scope=scope,
        kind=kind,
        title=title,
        body=body,
    )
    relay = _get_relay()
    return await relay.call("memory_write", agent_id, scope, inp.model_dump())


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def serve() -> None:
    """Start the MCP stdio server (blocks until stdin closes)."""
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(serve())
