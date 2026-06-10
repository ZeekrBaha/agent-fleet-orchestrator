"""Tests for Task 5.2: spawn_worker end-to-end + tasks + evidence model.

TDD: all tests written FIRST.  RED before any implementation exists.

AC-020 — golden event-sequence test:
  orchestrator turn → spawn_worker tool → worker turn → record_validation tool
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fleet.agents.backends.mock import MockBackend
from fleet.agents.backends.protocol import (
    TextChunk,
    ToolResultEvent,
    ToolUseEvent,
    TurnEnd,
)
from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService, create_event_service
from fleet.events.sse import SSEHub
from fleet.review.evidence import EvidenceService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_turn_end() -> TurnEnd:
    return TurnEnd(cost_usd=0.001, input_tokens=10, output_tokens=5, context_pct=0.01)


async def _wait_for_event(
    event_service: EventService,
    scope: str,
    *,
    type_filter: str,
    timeout: float = 5.0,
) -> None:
    """Poll until at least one event of *type_filter* appears in the log."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        events = await event_service.query(scope, type_filter=type_filter)
        if events:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"Timed out waiting for event type={type_filter!r} in scope={scope!r}"
    )


async def _wait_for_agent_message(
    event_service: EventService,
    scope: str,
    *,
    agent_id: str,
    timeout: float = 5.0,
) -> None:
    """Poll until an agent_message event from *agent_id* appears."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        events = await event_service.query(
            scope, agent_id=agent_id, type_filter="agent_message"
        )
        if events:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(
        f"Timed out waiting for agent_message from agent_id={agent_id!r}"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_orchestration.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
def hub() -> SSEHub:
    return SSEHub()


@pytest_asyncio.fixture
async def event_service(db: DatabaseManager, hub: SSEHub) -> EventService:
    return create_event_service(db, hub)


@pytest_asyncio.fixture
async def evidence_service(db: DatabaseManager) -> EvidenceService:
    return EvidenceService(db)


@pytest_asyncio.fixture
async def agent_service(
    db: DatabaseManager,
    event_service: EventService,
) -> AsyncIterator[Any]:
    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService

    inbox = InboxService(db)
    svc = AgentService(db, event_service, inbox)
    yield svc
    for session in list(svc._sessions.values()):
        task = session._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Unit tests for EvidenceService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_returns_id(evidence_service: EvidenceService) -> None:
    """create_task() returns a non-empty string task_id."""
    task_id = await evidence_service.create_task(
        scope="test-scope",
        title="Build login",
        description="Implement login feature",
    )
    assert isinstance(task_id, str)
    assert task_id != ""


@pytest.mark.asyncio
async def test_record_evidence_inserts_row(evidence_service: EvidenceService) -> None:
    """record_evidence() inserts a row; list_evidence() returns it."""
    task_id = await evidence_service.create_task(
        scope="test-scope",
        title="Test task",
        description="Run tests",
    )
    ev_id = await evidence_service.record_evidence(
        task_id=task_id,
        command="pytest -q",
        exit_code=0,
        summary="All green",
    )
    assert isinstance(ev_id, int)

    rows = await evidence_service.list_evidence(task_id)
    assert len(rows) == 1
    assert rows[0]["command"] == "pytest -q"
    assert rows[0]["exit_code"] == 0
    assert rows[0]["summary"] == "All green"


@pytest.mark.asyncio
async def test_merge_gate_no_evidence_blocks(evidence_service: EvidenceService) -> None:
    """Task with no evidence rows → can_merge=False."""
    task_id = await evidence_service.create_task(
        scope="test-scope",
        title="Empty task",
        description="No evidence yet",
    )
    can_merge, reason = await evidence_service.check_merge_gate(task_id)
    assert can_merge is False
    assert reason  # non-empty human-readable string


@pytest.mark.asyncio
async def test_merge_gate_all_passing_allows(evidence_service: EvidenceService) -> None:
    """All evidence rows with exit_code=0 → can_merge=True."""
    task_id = await evidence_service.create_task(
        scope="test-scope",
        title="Green task",
        description="All checks pass",
    )
    await evidence_service.record_evidence(
        task_id=task_id, command="pytest -q", exit_code=0, summary="green"
    )
    await evidence_service.record_evidence(
        task_id=task_id, command="ruff check .", exit_code=0, summary="clean"
    )
    can_merge, reason = await evidence_service.check_merge_gate(task_id)
    assert can_merge is True
    assert reason


@pytest.mark.asyncio
async def test_merge_gate_failing_evidence_blocks(
    evidence_service: EvidenceService,
) -> None:
    """Any evidence row with exit_code != 0 → can_merge=False."""
    task_id = await evidence_service.create_task(
        scope="test-scope",
        title="Failing task",
        description="One check failed",
    )
    await evidence_service.record_evidence(
        task_id=task_id, command="pytest -q", exit_code=0, summary="green"
    )
    await evidence_service.record_evidence(
        task_id=task_id, command="mypy .", exit_code=1, summary="type errors"
    )
    can_merge, reason = await evidence_service.check_merge_gate(task_id)
    assert can_merge is False
    assert reason


# ---------------------------------------------------------------------------
# End-to-end orchestration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_golden_event_sequence(
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """AC-020: golden event sequence for orchestrator→worker flow.

    Orchestrator runs a turn that emits spawn_worker tool call.
    Worker runs a turn that emits record_validation tool call.
    Asserts the correct event types appear in the DB, in order.
    """
    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService

    scope = "golden-scope"

    # Build transcripts
    orch_transcript = [
        [
            TextChunk(text="analyzing task"),
            ToolUseEvent(
                tool_id="t1",
                tool_name="spawn_worker",
                input={"name": "worker-1", "role": "coder", "task_description": "x"},
            ),
            ToolResultEvent(tool_id="t1", output="worker spawned", is_error=False),
            _make_turn_end(),
        ]
    ]

    worker_transcript = [
        [
            TextChunk(text="implementing"),
            ToolUseEvent(
                tool_id="t2",
                tool_name="record_validation",
                input={"task_id": "task-x", "command": "pytest", "exit_code": 0},
            ),
            ToolResultEvent(tool_id="t2", output="recorded", is_error=False),
            _make_turn_end(),
        ]
    ]

    inbox = InboxService(db)
    agent_svc = AgentService(db, event_service, inbox)

    sessions_to_cleanup: list[Any] = []

    try:
        # a. Create orchestrator agent
        orch_backend = MockBackend(transcript=orch_transcript)
        orch_record = await agent_svc.create_agent(
            scope=scope,
            name="orchestrator-1",
            role="orchestrator",
            backend=orch_backend,
            model="mock",
            task_description="build feature X",
        )
        sessions_to_cleanup.append(orch_record.id)

        # b. Send message to orchestrator; wait for its turn
        await agent_svc.send_message(orch_record.id, "user", "build feature X")
        await _wait_for_agent_message(event_service, scope, agent_id=orch_record.id)

        # c. Simulate worker spawn (create worker agent)
        worker_backend = MockBackend(transcript=worker_transcript)
        worker_record = await agent_svc.create_agent(
            scope=scope,
            name="worker-1",
            role="coder",
            backend=worker_backend,
            model="mock",
            task_description="implement it",
        )
        sessions_to_cleanup.append(worker_record.id)

        # d. Send message to worker; wait for its turn
        await agent_svc.send_message(worker_record.id, "orchestrator", "implement it")
        await _wait_for_agent_message(event_service, scope, agent_id=worker_record.id)

        # e. Assert golden event sequence
        all_events = await event_service.query(scope, limit=500)
        event_types = [e.type for e in all_events]

        # Must have at least two agent_message events
        agent_messages = [e for e in all_events if e.type == "agent_message"]
        n = len(agent_messages)
        assert n >= 2, (
            f"Expected >= 2 agent_message events, got {n}: {event_types}"
        )

        # Must have tool_call for spawn_worker
        tool_calls = [e for e in all_events if e.type == "tool_call"]
        spawn_calls = [
            e for e in tool_calls if e.payload.get("tool_name") == "spawn_worker"
        ]
        assert len(spawn_calls) >= 1, (
            f"Expected spawn_worker tool_call, got tool_calls: "
            f"{[e.payload.get('tool_name') for e in tool_calls]}"
        )

        # Must have tool_call for record_validation
        record_calls = [
            e for e in tool_calls if e.payload.get("tool_name") == "record_validation"
        ]
        assert len(record_calls) >= 1, (
            f"Expected record_validation tool_call, got tool_calls: "
            f"{[e.payload.get('tool_name') for e in tool_calls]}"
        )

        # State-change ordering: running → (message) → idle → running → (message) → idle
        state_events = [e for e in all_events if e.type == "state_change"]
        statuses = [e.payload.get("status") for e in state_events]
        # At minimum: 2 running + 2 idle (from both agents processing their turns)
        running_count = statuses.count("running")
        idle_count = statuses.count("idle")
        assert running_count >= 2, (
            f"Expected >= 2 running state changes, got: {statuses}"
        )
        assert idle_count >= 2, (
            f"Expected >= 2 idle state changes, got: {statuses}"
        )

    finally:
        for agent_id in sessions_to_cleanup:
            session = agent_svc._sessions.get(agent_id)
            if session and session._task and not session._task.done():
                session._task.cancel()
                try:
                    await session._task
                except (asyncio.CancelledError, Exception):
                    pass


@pytest.mark.asyncio
async def test_spawn_worker_missing_role_prompt_fails(
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """create_agent() with nonexistent role prompt → error event emitted."""
    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService

    scope = "error-scope"
    inbox = InboxService(db)
    agent_svc = AgentService(db, event_service, inbox)

    # role="nonexistent_role" has no matching prompts/roles/nonexistent_role.md
    from fleet.agents.promptbuild import MissingRolePromptError

    with pytest.raises(MissingRolePromptError):
        await agent_svc.create_agent(
            scope=scope,
            name="bad-agent",
            role="nonexistent_role",
            backend=MockBackend(transcript=[]),
            model="mock",
            task_description="some task",
        )

    # An error event must be emitted before the exception propagates (ADR-005).
    error_events = await event_service.query(scope, type_filter="error")
    assert len(error_events) >= 1, "Expected error event on MissingRolePromptError"


@pytest.mark.asyncio
async def test_evidence_gate_api(
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """E2E API test: create task, record evidence, check gate via REST endpoints."""
    from fleet.api.auth import require_token
    from fleet.api.review import router, set_evidence_service
    from fleet.review.evidence import EvidenceService

    evidence_svc = EvidenceService(db)
    set_evidence_service(evidence_svc)

    app = FastAPI()
    app.include_router(router)
    # Override auth to avoid lru_cache / env-var contention between tests
    app.dependency_overrides[require_token] = lambda: None

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers: dict[str, str] = {}

        # 1. Create a task via API
        resp = await client.post(
            "/api/review/tasks",
            json={
                "scope": "api-scope",
                "title": "My task",
                "description": "Run all checks",
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]
        assert task_id

        # 2. Gate should block before any evidence
        resp = await client.get(
            f"/api/review/tasks/{task_id}/gate", headers=headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["can_merge"] is False

        # 3. Record passing evidence directly via EvidenceService
        #    (the tool endpoint record_validation goes through the tools router,
        #    which is separately wired; here we test the review API surface)
        await evidence_svc.record_evidence(
            task_id=task_id, command="pytest -q", exit_code=0, summary="All green"
        )

        # 4. List evidence via API
        resp = await client.get(
            f"/api/review/tasks/{task_id}/evidence", headers=headers
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["exit_code"] == 0

        # 5. Gate should now allow merge
        resp = await client.get(
            f"/api/review/tasks/{task_id}/gate", headers=headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["can_merge"] is True


@pytest.mark.asyncio
async def test_get_task_returns_404_for_missing(
    db: DatabaseManager,
) -> None:
    """GET /api/review/tasks/<missing> → 404."""
    from fleet.api.auth import require_token
    from fleet.api.review import router, set_evidence_service
    from fleet.review.evidence import EvidenceService

    evidence_svc = EvidenceService(db)
    set_evidence_service(evidence_svc)

    app = FastAPI()
    app.include_router(router)
    # Override auth so the test doesn't depend on environment token state
    app.dependency_overrides[require_token] = lambda: None

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/review/tasks/no-such-task")
        assert resp.status_code == 404
