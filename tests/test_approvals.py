"""Tests for ApprovalService and the /api/approvals router (Task 6.2).

TDD: tests written BEFORE implementation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection, text

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService, create_event_service
from fleet.events.sse import SSEHub

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_approvals.db"))
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def hub() -> SSEHub:
    return SSEHub()


@pytest_asyncio.fixture
async def event_service(db: DatabaseManager, hub: SSEHub) -> EventService:
    return create_event_service(db, hub)


@pytest_asyncio.fixture
async def approval_service(db: DatabaseManager, event_service: EventService) -> Any:
    from fleet.approvals.service import ApprovalService

    svc = ApprovalService(db=db, event_service=event_service)
    await svc.load_pending()
    return svc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_agent(
    db: DatabaseManager,
    agent_id: str,
    scope: str,
    *,
    budget_hard_usd: float | None = None,
) -> None:
    """Insert a minimal agent row for tests that need a real agent in DB."""
    now = datetime.now(UTC).isoformat()

    def _write(conn: Connection) -> None:
        conn.execute(
            text(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  budget_hard_usd, created_at, updated_at)"
                " VALUES"
                " (:id, :name, :scope, :role, :backend, :model, 'idle',"
                "  :budget_hard_usd, :created_at, :updated_at)"
            ),
            {
                "id": agent_id,
                "name": agent_id,
                "scope": scope,
                "role": "worker",
                "backend": "mock",
                "model": "mock",
                "budget_hard_usd": budget_hard_usd,
                "created_at": now,
                "updated_at": now,
            },
        )
        conn.commit()

    await db.write(_write)


# ---------------------------------------------------------------------------
# Unit tests: ApprovalService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_request_creates_row(
    approval_service: Any,
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """request() inserts a pending approval row and emits an approval_request event."""
    approval_id = await approval_service.request(
        scope="scope-a",
        agent_id="agent-1",
        action="over_budget_continue",
        description="Agent exceeded hard budget",
    )

    assert approval_id != ""

    # Row should exist with status=pending
    with db.read_connection() as conn:
        row = conn.execute(
            text(
                "SELECT status, scope, requester_agent_id, operation"
                " FROM approvals WHERE id = :id"
            ),
            {"id": approval_id},
        ).fetchone()

    assert row is not None
    assert row.status == "pending"
    assert row.scope == "scope-a"
    assert row.requester_agent_id == "agent-1"
    assert row.operation == "over_budget_continue"

    # approval_request event must exist
    events = await event_service.query("scope-a", type_filter="approval_request")
    assert len(events) == 1
    assert events[0].payload["approval_id"] == approval_id
    assert events[0].payload["action"] == "over_budget_continue"


@pytest.mark.asyncio
async def test_approval_decide_approve(
    approval_service: Any,
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """decide(id, 'approve') updates status to approved and emits approval_decision."""
    approval_id = await approval_service.request(
        scope="scope-b",
        agent_id="agent-2",
        action="delete_worktree",
        description="Delete worktree for agent-2",
    )

    record = await approval_service.decide(approval_id, "approve", comment="Looks good")

    assert record.status == "approved"
    assert record.comment == "Looks good"
    assert record.decided_at is not None

    # DB row updated
    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT status, comment FROM approvals WHERE id = :id"),
            {"id": approval_id},
        ).fetchone()
    assert row is not None
    assert row.status == "approved"
    assert row.comment == "Looks good"

    # approval_decision event emitted
    events = await event_service.query("scope-b", type_filter="approval_decision")
    assert len(events) == 1
    assert events[0].payload["approval_id"] == approval_id
    assert events[0].payload["decision"] == "approve"


@pytest.mark.asyncio
async def test_approval_decide_deny(
    approval_service: Any,
    db: DatabaseManager,
    event_service: EventService,
) -> None:
    """decide(id, 'deny') updates status to denied and emits approval_decision event."""
    approval_id = await approval_service.request(
        scope="scope-c",
        agent_id="agent-3",
        action="deploy",
        description="Deploy to production",
    )

    record = await approval_service.decide(approval_id, "deny", comment="Too risky")

    assert record.status == "denied"
    assert record.comment == "Too risky"
    assert record.decided_at is not None

    events = await event_service.query("scope-c", type_filter="approval_decision")
    assert len(events) == 1
    assert events[0].payload["decision"] == "deny"


@pytest.mark.asyncio
async def test_approval_wait_resolves_on_decide(
    approval_service: Any,
) -> None:
    """wait_for_decision() resolves with 'approve' when decide() is called."""
    approval_id = await approval_service.request(
        scope="scope-d",
        agent_id="agent-4",
        action="merge",
        description="Merge feature branch",
    )

    # Concurrently wait and decide — wait should resolve with "approve"
    async def _decide_after_yield() -> None:
        await asyncio.sleep(0)  # yield once so wait_for_decision starts
        await approval_service.decide(approval_id, "approve")

    result, _ = await asyncio.gather(
        approval_service.wait_for_decision(approval_id, timeout_s=5.0),
        _decide_after_yield(),
    )

    assert result == "approve"


@pytest.mark.asyncio
async def test_approval_blocks_until_decided(
    approval_service: Any,
) -> None:
    """wait_for_decision() blocks until decide() is called (AC-033)."""
    approval_id = await approval_service.request(
        scope="scope-blocks",
        agent_id="agent-blocks",
        action="test_action",
        description="Test blocking behaviour",
    )

    resolved: list[str] = []

    async def waiter() -> None:
        result = await approval_service.wait_for_decision(approval_id, timeout_s=5.0)
        resolved.append(result)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)  # Let waiter start
    assert len(resolved) == 0, "should not have resolved before decision"

    await approval_service.decide(approval_id, "approve")
    await asyncio.wait_for(task, timeout=2.0)
    assert resolved == ["approve"]


@pytest.mark.asyncio
async def test_approval_wait_timeout(
    approval_service: Any,
) -> None:
    """wait_for_decision() raises ApprovalTimeoutError when no decision arrives."""
    from fleet.approvals.service import ApprovalTimeoutError

    approval_id = await approval_service.request(
        scope="scope-e",
        agent_id="agent-5",
        action="over_budget_continue",
        description="Timed-out approval",
    )

    with pytest.raises(ApprovalTimeoutError):
        await approval_service.wait_for_decision(approval_id, timeout_s=0.05)


@pytest.mark.asyncio
async def test_approval_list_pending(
    approval_service: Any,
) -> None:
    """list_pending(scope) returns only pending rows for that scope."""
    # Create two approvals in scope-f
    id1 = await approval_service.request(
        scope="scope-f",
        agent_id="agent-6a",
        action="delete_worktree",
        description="Delete wt 1",
    )
    id2 = await approval_service.request(
        scope="scope-f",
        agent_id="agent-6b",
        action="deploy",
        description="Deploy",
    )
    # Create one in a different scope
    await approval_service.request(
        scope="other-scope",
        agent_id="agent-6c",
        action="merge",
        description="Merge",
    )

    # Approve id1
    await approval_service.decide(id1, "approve")

    # list_pending should return only the still-pending row in scope-f
    pending = await approval_service.list_pending("scope-f")
    assert len(pending) == 1
    assert pending[0].id == id2
    assert pending[0].status == "pending"


@pytest.mark.asyncio
async def test_approval_get(
    approval_service: Any,
) -> None:
    """get(approval_id) returns the ApprovalRecord, or None for unknown id."""
    approval_id = await approval_service.request(
        scope="scope-g",
        agent_id="agent-7",
        action="over_budget_continue",
        description="Some action",
    )

    record = await approval_service.get(approval_id)
    assert record is not None
    assert record.id == approval_id
    assert record.status == "pending"

    missing = await approval_service.get("nonexistent-id")
    assert missing is None


# ---------------------------------------------------------------------------
# Integration test: AgentSession budget-pause → approval flow
# ---------------------------------------------------------------------------


async def _wait_for_status(
    agent_id: str,
    target_status: str,
    service: Any,
    timeout: float = 3.0,
) -> Any:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        agent = await service.get_agent(agent_id)
        if agent and agent.status == target_status:
            return agent
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Agent {agent_id!r} never reached status {target_status!r}")


async def _wait_for_event(
    scope: str,
    event_type: str,
    svc: EventService,
    timeout: float = 3.0,
) -> list[Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        events = await svc.query(scope, type_filter=event_type)
        if events:
            return events
        await asyncio.sleep(0.05)
    raise TimeoutError(f"No {event_type!r} event in {timeout}s")


@pytest_asyncio.fixture
async def services(
    db: DatabaseManager, event_service: EventService
) -> AsyncIterator[dict[str, Any]]:
    """Build AgentService + ApprovalService wired together for session tests."""
    from fleet.agents.inbox import InboxService
    from fleet.agents.service import AgentService
    from fleet.approvals.service import ApprovalService

    inbox = InboxService(db)
    approval_svc = ApprovalService(db=db, event_service=event_service)
    await approval_svc.load_pending()

    agent_svc = AgentService(
        db=db,
        event_service=event_service,
        inbox_service=inbox,
        approval_svc=approval_svc,
    )

    yield {
        "agent_svc": agent_svc,
        "approval_svc": approval_svc,
        "event_svc": event_service,
    }

    # Teardown: cancel session tasks
    for session in agent_svc._sessions.values():
        task = session._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_budget_pause_approval_flow(
    services: dict[str, Any],
) -> None:
    """Agent with hard budget → paused_budget → approve → resumes (budget_approved event)."""  # noqa: E501
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.backends.protocol import TextChunk, TurnEnd

    agent_svc = services["agent_svc"]
    approval_svc = services["approval_svc"]
    event_svc = services["event_svc"]

    scope = "scope-budget-pause"
    backend = MockBackend(
        transcript=[
            [
                TextChunk(text="working"),
                TurnEnd(
                    cost_usd=5.0,  # exceeds hard limit of 1.0
                    input_tokens=10,
                    output_tokens=10,
                    context_pct=0.1,
                ),
            ],
            [  # second turn after resume
                TextChunk(text="resumed"),
                TurnEnd(
                    cost_usd=0.0,
                    input_tokens=5,
                    output_tokens=5,
                    context_pct=0.05,
                ),
            ],
        ]
    )

    record = await agent_svc.create_agent(
        scope=scope,
        name="budget-agent",
        role="coder",
        backend=backend,
        model="mock-1",
        budget_hard_usd=1.0,
    )

    # Send a message to trigger the first turn
    await agent_svc.send_message(record.id, "user", "do something expensive")

    # Wait for agent to pause on budget
    await _wait_for_status(record.id, "paused_budget", agent_svc, timeout=5.0)

    # approval_request event must exist (Fix 3: AC-032 assertion)
    approval_events = await _wait_for_event(
        scope, "approval_request", event_svc, timeout=3.0
    )
    assert len(approval_events) >= 1, (
        "approval_request event must exist before decision"
    )
    assert approval_events[0].payload.get("action") == "budget_exceeded"
    approval_id = str(approval_events[0].payload["approval_id"])

    # Approve the request
    await approval_svc.decide(approval_id, "approve")

    # Poll until state_change with status=budget_approved appears.
    # (generic state_change events already exist, so _wait_for_event would return early)
    deadline = asyncio.get_event_loop().time() + 5.0
    budget_approved: list[Any] = []
    while asyncio.get_event_loop().time() < deadline:
        all_sc = await event_svc.query(scope, type_filter="state_change")
        budget_approved = [
            e for e in all_sc if e.payload.get("status") == "budget_approved"
        ]
        if budget_approved:
            break
        await asyncio.sleep(0.05)
    assert len(budget_approved) >= 1, "Expected budget_approved state_change event"


@pytest.mark.asyncio
async def test_budget_deny_stops_agent(
    services: dict[str, Any],
) -> None:
    """Deny budget approval → agent emits budget_denied state_change."""
    from fleet.agents.backends.mock import MockBackend
    from fleet.agents.backends.protocol import TextChunk, TurnEnd

    agent_svc = services["agent_svc"]
    approval_svc = services["approval_svc"]
    event_svc = services["event_svc"]

    scope = "scope-budget-deny"
    backend = MockBackend(
        transcript=[
            [
                TextChunk(text="working"),
                TurnEnd(
                    cost_usd=5.0,  # exceeds hard limit of 1.0
                    input_tokens=10,
                    output_tokens=10,
                    context_pct=0.1,
                ),
            ],
        ]
    )

    record = await agent_svc.create_agent(
        scope=scope,
        name="deny-agent",
        role="coder",
        backend=backend,
        model="mock-1",
        budget_hard_usd=1.0,
    )

    # Send a message to trigger the first turn
    await agent_svc.send_message(record.id, "user", "do something expensive")

    # Wait for agent to pause on budget
    await _wait_for_status(record.id, "paused_budget", agent_svc, timeout=5.0)

    # Get approval_id from event
    approval_events = await _wait_for_event(
        scope, "approval_request", event_svc, timeout=3.0
    )
    approval_id = str(approval_events[0].payload["approval_id"])

    # Deny the request
    await approval_svc.decide(approval_id, "deny")

    # Agent should emit state_change with status=budget_denied.
    deadline = asyncio.get_event_loop().time() + 5.0
    budget_denied: list[Any] = []
    while asyncio.get_event_loop().time() < deadline:
        all_sc = await event_svc.query(scope, type_filter="state_change")
        budget_denied = [
            e for e in all_sc if e.payload.get("status") == "budget_denied"
        ]
        if budget_denied:
            break
        await asyncio.sleep(0.05)
    assert len(budget_denied) >= 1, "Expected budget_denied state_change event"

    # After deny: verify no budget_approved event was emitted (AC-033)
    all_sc = await event_svc.query(scope, type_filter="state_change")
    approved_events = [
        e for e in all_sc if e.payload.get("status") == "budget_approved"
    ]
    assert len(approved_events) == 0, "budget_approved must not be emitted after deny"


# ---------------------------------------------------------------------------
# API tests: /api/approvals
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_client(
    db: DatabaseManager, event_service: EventService
) -> AsyncIterator[tuple[AsyncClient, Any]]:
    """Build a minimal FastAPI app with the approvals router wired."""
    from fleet.api.approvals import router, set_approval_service
    from fleet.api.auth import require_token
    from fleet.approvals.service import ApprovalService

    approval_svc = ApprovalService(db=db, event_service=event_service)
    await approval_svc.load_pending()
    set_approval_service(approval_svc)

    app = FastAPI()
    app.include_router(router)

    async def _no_auth() -> None:
        return None

    app.dependency_overrides[require_token] = _no_auth

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, approval_svc


@pytest.mark.asyncio
async def test_approvals_api_list(
    api_client: tuple[AsyncClient, Any],
    event_service: EventService,
) -> None:
    """GET /api/approvals?scope=x returns pending approvals for that scope."""
    client, approval_svc = api_client

    # Create two approvals in scope-api-list
    await approval_svc.request(
        scope="scope-api-list",
        agent_id="agent-api-1",
        action="delete_worktree",
        description="Delete worktree",
    )
    await approval_svc.request(
        scope="scope-api-list",
        agent_id="agent-api-2",
        action="deploy",
        description="Deploy to prod",
    )
    # One in a different scope
    await approval_svc.request(
        scope="other-api-scope",
        agent_id="agent-api-3",
        action="merge",
        description="Merge branch",
    )

    response = await client.get("/api/approvals?scope=scope-api-list")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert all(r["scope"] == "scope-api-list" for r in data)
    assert all(r["status"] == "pending" for r in data)


@pytest.mark.asyncio
async def test_approvals_api_decide(
    api_client: tuple[AsyncClient, Any],
    event_service: EventService,
) -> None:
    """POST /api/approvals/{id}/decide returns updated record and emits event."""
    client, approval_svc = api_client

    approval_id = await approval_svc.request(
        scope="scope-api-decide",
        agent_id="agent-api-d1",
        action="over_budget_continue",
        description="Budget exceeded",
    )

    response = await client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"decision": "approve", "comment": "OK from test"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "approved"
    assert data["id"] == approval_id
    assert data["comment"] == "OK from test"

    # approval_decision event emitted
    events = await event_service.query(
        "scope-api-decide", type_filter="approval_decision"
    )
    assert len(events) == 1
    assert events[0].payload["decision"] == "approve"
