"""Tests for fleet/db.py — written BEFORE the implementation (TDD RED phase).

Run unit tests:  uv run pytest tests/test_db.py -q -m "not slow"
Run load test:   uv run pytest tests/test_db.py -q -m "slow" -v
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from fleet.db import DatabaseManager, init_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _insert_event(db: DatabaseManager, scope: str = "test") -> int:
    """Insert one event row and return its rowid."""

    def op(conn):  # type: ignore[no-untyped-def]
        from sqlalchemy import text

        result = conn.execute(
            text(
                "INSERT INTO events (ts, scope, type, summary, payload_json) "
                "VALUES (:ts, :scope, :type, :summary, :payload_json)"
            ),
            {
                "ts": _now(),
                "scope": scope,
                "type": "test_event",
                "summary": "unit test",
                "payload_json": "{}",
            },
        )
        conn.commit()
        return result.lastrowid

    return await db.write(op)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path):
    """Running migrations twice on the same DB must not raise."""
    db_path = str(tmp_path / "fleet_test.db")
    db = await init_db(db_path)
    try:
        # run_migrations is called internally by init_db; call it again explicitly
        from fleet.db import run_migrations

        with db.read_connection() as conn:
            run_migrations(conn)
        # No exception means idempotent
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_writer_ordering(tmp_path):
    """3 writes submitted concurrently must all commit; none lost."""
    db_path = str(tmp_path / "fleet_test.db")
    db = await init_db(db_path)
    try:
        # Submit three concurrent writes
        results = await asyncio.gather(
            _insert_event(db, "s1"),
            _insert_event(db, "s2"),
            _insert_event(db, "s3"),
        )
        # All three should have returned a valid rowid
        assert len(results) == 3
        assert all(r is not None and r > 0 for r in results)
        # All three rows actually exist in the DB
        with db.read_connection() as conn:
            from sqlalchemy import text

            row = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'test_event'")
            ).scalar()
        assert row == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_read_does_not_block_concurrent_reads(tmp_path):
    """Two read connections open simultaneously must not error."""
    db_path = str(tmp_path / "fleet_test.db")
    db = await init_db(db_path)
    try:
        with db.read_connection() as conn1, db.read_connection() as conn2:
            from sqlalchemy import text

            r1 = conn1.execute(text("SELECT 1")).scalar()
            r2 = conn2.execute(text("SELECT 1")).scalar()
        assert r1 == 1
        assert r2 == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_close_drains_queue(tmp_path):
    """A write enqueued before close() must complete before close() returns."""
    db_path = str(tmp_path / "fleet_test.db")
    db = await init_db(db_path)

    # Enqueue a write but do NOT await it yet
    write_task = asyncio.create_task(_insert_event(db))

    # Close immediately — must drain the pending write
    await db.close()

    # The write task should now be done (close waited for it)
    assert write_task.done(), "write task should be completed after close()"
    rowid = write_task.result()
    assert rowid is not None and rowid > 0


# ---------------------------------------------------------------------------
# Load test
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.asyncio
async def test_load_25_producers_200_events(tmp_path):
    """25 async producers × 200 events = 5000 rows; p95 write latency ≤ 5 ms."""
    db_path = str(tmp_path / "fleet_load.db")
    db = await init_db(db_path)

    latencies: list[float] = []
    lock = asyncio.Lock()

    async def producer(producer_id: int) -> None:
        for _ in range(200):
            t0 = time.perf_counter()
            await _insert_event(db, scope=f"producer-{producer_id}")
            elapsed_ms = (time.perf_counter() - t0) * 1000
            async with lock:
                latencies.append(elapsed_ms)

    try:
        await asyncio.gather(*[producer(i) for i in range(25)])

        # Verify zero loss
        with db.read_connection() as conn:
            from sqlalchemy import text

            total = conn.execute(text("SELECT COUNT(*) FROM events")).scalar()
        assert total == 5000, f"expected 5000 rows, got {total}"

        # p95 latency ≤ 5 ms
        sorted_latencies = sorted(latencies)
        p95_index = int(len(sorted_latencies) * 0.95)
        p95 = sorted_latencies[p95_index]
        print(f"\np95 write latency: {p95:.3f} ms  (over {len(latencies)} writes)")
        assert p95 <= 5.0, f"p95 latency {p95:.3f} ms exceeds 5 ms limit"
    finally:
        await db.close()
