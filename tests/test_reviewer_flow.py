"""Tests for Task 6.3: Reviewer role + merge execution wiring.

TDD: all tests are written BEFORE the implementation exists.
Tests must fail (RED) before changes to tool_handlers.py, evidence.py, etc.

Test groups:
  1. review_verdict event emission
  2. merge gate with reviewer verdict rows
  3. execute_merge tool dispatching
  4. execute_merge approval gate
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fleet.api.auth import AgentIdentity, require_agent_identity
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _no_auth() -> AgentIdentity:
    """Bypass token auth in tests (admin impersonation)."""
    return AgentIdentity(agent_id=None, role=None, is_admin=True)


def _make_permissive_policy(extra_tools: list[str] | None = None) -> Any:
    """Return a PolicyService with a test_role that allows all tools."""
    from fleet.policy.rules import ManifestConfig, RoleConfig
    from fleet.policy.service import PolicyService

    base_tools = [
        "spawn_worker",
        "send_message",
        "list_agents",
        "get_agent_logs",
        "stop_agent",
        "worker_wip",
        "check_conflict",
        "record_validation",
        "report_issue",
        "update_progress",
        "request_approval",
        "memory_write",
        "execute_merge",
    ]
    if extra_tools:
        base_tools = list(set(base_tools) | set(extra_tools))

    manifest = ManifestConfig(
        version="1",
        roles={
            "test_role": RoleConfig(allowed_tools=base_tools),
            "reviewer": RoleConfig(allowed_tools=base_tools),
            "orchestrator": RoleConfig(allowed_tools=base_tools),
        },
    )
    return PolicyService(manifest)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "reviewer_test.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture()
async def event_service(db: DatabaseManager) -> EventService:
    hub = SSEHub()
    return EventService(db, hub)


@pytest_asyncio.fixture()
async def evidence_svc(db: DatabaseManager) -> Any:
    from fleet.review.evidence import EvidenceService

    return EvidenceService(db, gate_require_reviewer=False)


def _build_tools_app(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any = None,
    merge_svc: Any = None,
    agent_svc: Any = None,
) -> FastAPI:
    """Build a minimal FastAPI app wired with the tools router."""
    from fleet.api.tools import router, set_policy_service, set_tool_services
    from fleet.review.evidence import EvidenceService

    _evidence_svc = (
        evidence_svc
        if evidence_svc is not None
        else EvidenceService(db, gate_require_reviewer=False)
    )

    set_tool_services(
        agent_svc=agent_svc or _make_mock_agent_svc(role="test_role"),
        event_svc=event_service,
        workspace_svc=None,
        worktree_svc=None,
        db=db,
        evidence_svc=_evidence_svc,
        merge_svc=merge_svc,
    )
    set_policy_service(_make_permissive_policy())

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_agent_identity] = _no_auth
    return app


def _make_mock_agent_svc(role: str = "test_role") -> Any:
    """Return a mock AgentService that returns an agent with the given role."""
    mock_agent = MagicMock()
    mock_agent.role = role
    mock_agent.status = "idle"

    mock_agent_svc = AsyncMock()
    mock_agent_svc.get_agent.return_value = mock_agent
    mock_agent_svc.list_agents.return_value = []
    return mock_agent_svc


# ---------------------------------------------------------------------------
# 1. review_verdict event emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reviewer_record_validation_emits_review_verdict(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
) -> None:
    """Reviewer record_validation(check_name='review') emits review_verdict."""
    # Create a task to record evidence against
    task_id = await evidence_svc.create_task(
        scope="test-scope",
        title="Review task",
        description="Feature to review",
    )

    # Agent with role=reviewer
    reviewer_agent_svc = _make_mock_agent_svc(role="reviewer")

    app = _build_tools_app(
        db, event_service, evidence_svc, agent_svc=reviewer_agent_svc
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/record_validation",
            json={
                "agent_id": "reviewer-agent-1",
                "scope": "test-scope",
                "task_id": task_id,
                "check_name": "review",
                "status": "pass",
                "output": "All tests present, no security issues.",
            },
        )

    assert resp.status_code == 200, resp.text

    # review_verdict event must be emitted
    events = await event_service.query("test-scope", type_filter="review_verdict")
    assert len(events) == 1, f"Expected 1 review_verdict event, got {len(events)}"
    ev = events[0]
    assert ev.payload["verdict"] == "pass"
    assert ev.payload["task_id"] == task_id
    assert "review" in ev.summary.lower()


@pytest.mark.asyncio
async def test_non_reviewer_record_validation_no_review_verdict(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
) -> None:
    """Non-reviewer calling record_validation(check_name='review') emits no verdict."""
    task_id = await evidence_svc.create_task(
        scope="test-scope",
        title="Coder task",
        description="desc",
    )

    # Agent with role=orchestrator — same check_name "review" but not "reviewer" role
    other_agent_svc = _make_mock_agent_svc(role="orchestrator")

    app = _build_tools_app(db, event_service, evidence_svc, agent_svc=other_agent_svc)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/record_validation",
            json={
                "agent_id": "other-agent-1",
                "scope": "test-scope",
                "task_id": task_id,
                "check_name": "review",
                "status": "pass",
                "output": "non-reviewer recording a review check",
            },
        )

    assert resp.status_code == 200, resp.text

    # No review_verdict event — only reviewer role triggers it
    events = await event_service.query("test-scope", type_filter="review_verdict")
    assert len(events) == 0, f"Expected 0 review_verdict events, got {len(events)}"


@pytest.mark.asyncio
async def test_reviewer_non_review_check_no_review_verdict(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
) -> None:
    """Reviewer with check_name != 'review' does not emit review_verdict."""
    task_id = await evidence_svc.create_task(
        scope="test-scope",
        title="Reviewer task",
        description="desc",
    )

    reviewer_agent_svc = _make_mock_agent_svc(role="reviewer")

    app = _build_tools_app(
        db, event_service, evidence_svc, agent_svc=reviewer_agent_svc
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/record_validation",
            json={
                "agent_id": "reviewer-agent-2",
                "scope": "test-scope",
                "task_id": task_id,
                "check_name": "pytest",
                "status": "pass",
                "output": "all green",
            },
        )

    assert resp.status_code == 200, resp.text

    events = await event_service.query("test-scope", type_filter="review_verdict")
    assert len(events) == 0, f"Expected 0 review_verdict events, got {len(events)}"


# ---------------------------------------------------------------------------
# 2. merge gate with reviewer verdict rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_gate_passes_with_review_pass(
    evidence_svc: Any,
) -> None:
    """Task with review=pass row → gate returns (True, 'all checks passed')."""
    task_id = await evidence_svc.create_task(
        scope="test-scope",
        title="Reviewed task",
        description="desc",
    )
    # Record a non-review check and the review verdict
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "all green")
    await evidence_svc.record_evidence(task_id, "review", "pass", "looks good")

    can_merge, reason = await evidence_svc.check_merge_gate(task_id)

    assert can_merge is True, f"Expected gate open, got reason: {reason!r}"
    assert reason == "all checks passed"


@pytest.mark.asyncio
async def test_merge_gate_fails_with_review_fail(
    evidence_svc: Any,
) -> None:
    """Task with review=fail row → gate returns (False, 'reviewer verdict: fail')."""
    task_id = await evidence_svc.create_task(
        scope="test-scope",
        title="Failed review task",
        description="desc",
    )
    # Other checks pass but reviewer says fail
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "all green")
    await evidence_svc.record_evidence(task_id, "review", "fail", "missing tests")

    can_merge, reason = await evidence_svc.check_merge_gate(task_id)

    assert can_merge is False
    assert reason == "reviewer verdict: fail"


@pytest.mark.asyncio
async def test_merge_gate_no_review_row_still_passes(
    evidence_svc: Any,
) -> None:
    """Task with only non-review passing checks → gate passes (reviewer is optional)."""
    task_id = await evidence_svc.create_task(
        scope="test-scope",
        title="No review task",
        description="desc",
    )
    await evidence_svc.record_evidence(task_id, "pytest", "pass", "all green")
    await evidence_svc.record_evidence(task_id, "ruff", "pass", "no lint errors")

    can_merge, reason = await evidence_svc.check_merge_gate(task_id)

    assert can_merge is True
    assert reason == "all checks passed"


# ---------------------------------------------------------------------------
# 3. execute_merge tool dispatching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_merge_tool_dispatches(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
) -> None:
    """POST /api/tools/execute_merge → merge service called, commit_sha returned."""
    from fleet.review.merge import MergeResult

    mock_merge_svc = AsyncMock()
    mock_merge_svc.execute_merge.return_value = MergeResult(
        commit_sha="abc123def456",
        branch="fleet/task-foo",
        task_id="task-001",
    )

    orchestrator_agent_svc = _make_mock_agent_svc(role="orchestrator")
    app = _build_tools_app(
        db,
        event_service,
        evidence_svc,
        merge_svc=mock_merge_svc,
        agent_svc=orchestrator_agent_svc,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/execute_merge",
            json={
                "agent_id": "orch-agent-1",
                "scope": "test-scope",
                "worktree_id": "wt-001",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["commit_sha"] == "abc123def456"
    assert body["branch"] == "fleet/task-foo"
    assert body["task_id"] == "task-001"

    # Verify merge service was called with the correct worktree_id
    mock_merge_svc.execute_merge.assert_called_once()
    call_kwargs = mock_merge_svc.execute_merge.call_args
    assert call_kwargs.kwargs.get("worktree_id") == "wt-001" or (
        len(call_kwargs.args) > 0 and call_kwargs.args[0] == "wt-001"
    )


@pytest.mark.asyncio
async def test_execute_merge_tool_missing_merge_svc_returns_503(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
) -> None:
    """POST /api/tools/execute_merge without merge service wired → 503."""
    # Build app WITHOUT merge_svc
    app = _build_tools_app(db, event_service, evidence_svc, merge_svc=None)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/execute_merge",
            json={
                "agent_id": "orch-agent-1",
                "scope": "test-scope",
                "worktree_id": "wt-001",
            },
        )

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 4. execute_merge approval gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_merge_emits_tool_call_and_result_events(
    db: DatabaseManager,
    event_service: EventService,
    evidence_svc: Any,
) -> None:
    """execute_merge tool call emits tool_call and tool_result events."""
    from fleet.review.merge import MergeResult

    mock_merge_svc = AsyncMock()
    mock_merge_svc.execute_merge.return_value = MergeResult(
        commit_sha="sha-xyz",
        branch="fleet/task-bar",
        task_id="task-002",
    )

    orchestrator_agent_svc = _make_mock_agent_svc(role="orchestrator")
    app = _build_tools_app(
        db,
        event_service,
        evidence_svc,
        merge_svc=mock_merge_svc,
        agent_svc=orchestrator_agent_svc,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tools/execute_merge",
            json={
                "agent_id": "orch-agent-2",
                "scope": "audit-scope",
                "worktree_id": "wt-002",
            },
        )

    assert resp.status_code == 200, resp.text

    # tool_call event emitted
    call_events = await event_service.query("audit-scope", type_filter="tool_call")
    assert any(
        e.payload.get("tool_name") == "execute_merge" for e in call_events
    ), "Expected a tool_call event for execute_merge"

    # tool_result event emitted
    result_events = await event_service.query("audit-scope", type_filter="tool_result")
    assert any(
        e.payload.get("tool_name") == "execute_merge" for e in result_events
    ), "Expected a tool_result event for execute_merge"


# ---------------------------------------------------------------------------
# 5. Manifest — reviewer role has all required tools
# ---------------------------------------------------------------------------


def test_reviewer_manifest_has_required_tools() -> None:
    """default.yaml reviewer role must include the 5 required tools."""
    from fleet.policy.rules import load_default_manifest

    manifest = load_default_manifest()
    reviewer_tools = manifest.roles["reviewer"].allowed_tools
    required = {
        "record_validation",
        "worker_wip",
        "check_conflict",
        "get_agent_logs",
        "send_message",
    }
    missing = required - set(reviewer_tools)
    assert not missing, f"reviewer role missing tools: {missing}"
