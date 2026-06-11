"""P1-3: Append-only enforcement on the events audit log.

SQLite triggers must prevent UPDATE and DELETE on the events table.
Without triggers, a compromised or buggy component can silently rewrite history.

Tests (TDD-first):
  1. UPDATE on an events row raises an IntegrityError-class failure.
  2. DELETE on an events row raises an IntegrityError-class failure.
  3. INSERT still works (append-only, not read-only).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy.exc

from fleet.db import DatabaseManager, init_db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "audit_immutable.db")


@pytest_asyncio.fixture
async def db(db_path: str) -> Any:
    manager = await init_db(db_path)
    yield manager
    await manager.close()


async def _insert_event(db: DatabaseManager) -> int:
    """Insert one event row and return its id."""
    from sqlalchemy import text

    def _write(conn: Any) -> int:
        result = conn.execute(
            text(
                "INSERT INTO events (ts, scope, agent_id, type, summary, payload_json)"
                " VALUES ('2024-01-01T00:00:00Z', 'test', NULL, 'state_change',"
                "         'test event', '{}')"
            )
        )
        conn.commit()
        return result.lastrowid  # type: ignore[return-value]

    return await db.write(_write)


@pytest.mark.asyncio
async def test_audit_update_rejected(db: DatabaseManager) -> None:
    """UPDATE on events table must raise an integrity error."""
    from sqlalchemy import text

    event_id = await _insert_event(db)

    def _try_update(conn: Any) -> None:
        conn.execute(
            text("UPDATE events SET summary = 'tampered' WHERE id = :id"),
            {"id": event_id},
        )
        conn.commit()

    with pytest.raises(
        (sqlalchemy.exc.IntegrityError, sqlalchemy.exc.OperationalError),
        match=r"append.only|immutable",
    ):
        await db.write(_try_update)


@pytest.mark.asyncio
async def test_audit_delete_rejected(db: DatabaseManager) -> None:
    """DELETE on events table must raise an integrity error."""
    from sqlalchemy import text

    event_id = await _insert_event(db)

    def _try_delete(conn: Any) -> None:
        conn.execute(
            text("DELETE FROM events WHERE id = :id"),
            {"id": event_id},
        )
        conn.commit()

    with pytest.raises(
        (sqlalchemy.exc.IntegrityError, sqlalchemy.exc.OperationalError),
        match=r"append.only|immutable",
    ):
        await db.write(_try_delete)


@pytest.mark.asyncio
async def test_audit_insert_still_works(db: DatabaseManager) -> None:
    """INSERT into events must still succeed after triggers are added."""
    from sqlalchemy import text

    event_id = await _insert_event(db)

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT summary FROM events WHERE id = :id"),
            {"id": event_id},
        ).fetchone()

    assert row is not None
    assert row[0] == "test event"
