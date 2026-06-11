"""Tests for InboxService (Task 2.2).

TDD: these tests are written BEFORE the implementation exists.
All tests should FAIL before fleet/agents/inbox.py is created.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from fleet.db import DatabaseManager, init_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path) -> AsyncIterator[DatabaseManager]:
    manager = await init_db(str(tmp_path / "test_inbox.db"))
    yield manager
    await manager.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_creates_pending_row(db: DatabaseManager) -> None:
    """enqueue() returns an id; pending_count is 1 afterwards."""
    from fleet.agents.inbox import InboxService

    svc = InboxService(db)
    inbox_id = await svc.enqueue("agent-1", "orchestrator", "hello")

    assert isinstance(inbox_id, int)
    assert inbox_id > 0

    count = await svc.pending_count("agent-1")
    assert count == 1


@pytest.mark.asyncio
async def test_deliver_next_returns_oldest(db: DatabaseManager) -> None:
    """deliver_next() returns messages FIFO (oldest first)."""
    from fleet.agents.inbox import InboxService

    svc = InboxService(db)
    id1 = await svc.enqueue("agent-1", "user", "first")
    id2 = await svc.enqueue("agent-1", "user", "second")  # noqa: F841
    id3 = await svc.enqueue("agent-1", "user", "third")  # noqa: F841

    msg = await svc.deliver_next("agent-1")
    assert msg is not None
    assert msg.id == id1
    assert msg.message == "first"
    assert msg.status == "pending"


@pytest.mark.asyncio
async def test_mark_delivered_updates_row(db: DatabaseManager) -> None:
    """mark_delivered() sets status=delivered and delivered_at."""
    from fleet.agents.inbox import InboxService

    svc = InboxService(db)
    inbox_id = await svc.enqueue("agent-1", "user", "hello")

    msg = await svc.deliver_next("agent-1")
    assert msg is not None

    await svc.mark_delivered(inbox_id)

    # pending_count should be 0 now
    count = await svc.pending_count("agent-1")
    assert count == 0

    # Verify row directly via a read connection
    from sqlalchemy import text

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT status, delivered_at FROM inbox WHERE id = :id"),
            {"id": inbox_id},
        ).fetchone()
    assert row is not None
    assert row.status == "delivered"
    assert row.delivered_at is not None


@pytest.mark.asyncio
async def test_pending_messages_survive_restart(db: DatabaseManager) -> None:
    """Pending messages are not lost when a new InboxService instance is created."""
    from fleet.agents.inbox import InboxService

    svc1 = InboxService(db)
    await svc1.enqueue("agent-1", "orchestrator", "restart-me")

    # Simulate a restart by creating a new InboxService instance on the same DB
    svc2 = InboxService(db)
    count = await svc2.pending_count("agent-1")
    assert count == 1

    msg = await svc2.deliver_next("agent-1")
    assert msg is not None
    assert msg.message == "restart-me"
