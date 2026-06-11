"""Tests for fleet/db.py — written BEFORE the implementation (TDD RED phase).

Run unit tests:  uv run pytest tests/test_db.py -q -m "not slow"
Run load test:   uv run pytest tests/test_db.py -q -m "slow" -v
"""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3 as _sqlite3
import time
from datetime import UTC, datetime

import pytest

from fleet.db import MIGRATIONS_DIR, DatabaseManager, init_db

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

        run_migrations(db_path)  # second call must be a no-op
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
    """A write enqueued before close() must complete before close() returns.

    Per the close() contract, callers must ensure writes are actually enqueued
    (i.e. have reached db.write()) before calling close().  We yield once after
    create_task() to let the task run up to its first await (the queue.put),
    ensuring the item is queued before the stop sentinel is enqueued by close().
    """
    db_path = str(tmp_path / "fleet_test.db")
    db = await init_db(db_path)

    # Schedule the write task and yield so it runs up to its queue.put() call.
    write_task = asyncio.create_task(_insert_event(db))
    await asyncio.sleep(0)

    # Close — must drain the already-enqueued write before the stop sentinel.
    await db.close()

    # The write task should now be done (close waited for it)
    assert write_task.done(), "write task should be completed after close()"
    rowid = write_task.result()
    assert rowid is not None and rowid > 0


@pytest.mark.asyncio
async def test_write_error_propagates_and_writer_survives(tmp_path):
    """A write op that raises must propagate the exception to the caller.

    The writer task must remain alive so subsequent writes still succeed.
    """
    db_path = str(tmp_path / "fleet_test.db")
    db = await init_db(db_path)
    try:
        def boom_op(conn):  # type: ignore[no-untyped-def]
            raise ValueError("boom")

        # The exception must propagate to the caller.
        with pytest.raises(ValueError, match="boom"):
            await db.write(boom_op)

        # Writer task must still be alive — a subsequent valid write succeeds.
        rowid = await _insert_event(db, scope="after-error")
        assert rowid is not None and rowid > 0
    finally:
        await db.close()


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


# ---------------------------------------------------------------------------
# B1: Migration framework tests (RED — written before implementation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_schema_tracking_table_created(tmp_path: pathlib.Path) -> None:
    """init_db creates schema_migrations tracking table on a fresh DB."""
    db_path = str(tmp_path / "fleet.db")
    db = await init_db(db_path)
    await db.close()

    raw = _sqlite3.connect(db_path)
    try:
        rows = raw.execute(
            "SELECT name FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchall()
    finally:
        raw.close()
    assert rows, "schema_migrations table must exist after init_db"


@pytest.mark.asyncio
async def test_migration_records_applied_versions(tmp_path: pathlib.Path) -> None:
    """Fresh DB: 0001_init is recorded in schema_migrations after init_db."""
    db_path = str(tmp_path / "fleet.db")
    db = await init_db(db_path)
    await db.close()

    raw = _sqlite3.connect(db_path)
    try:
        versions = [r[0] for r in raw.execute("SELECT version FROM schema_migrations")]
    finally:
        raw.close()
    assert "0001_init" in versions


@pytest.mark.asyncio
async def test_migration_rerun_no_duplicate_records(tmp_path: pathlib.Path) -> None:
    """init_db called twice on same DB does not duplicate schema_migrations rows."""
    db_path = str(tmp_path / "fleet.db")
    db1 = await init_db(db_path)
    await db1.close()
    db2 = await init_db(db_path)
    await db2.close()

    raw = _sqlite3.connect(db_path)
    try:
        count = raw.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version='0001_init'"
        ).fetchone()[0]
    finally:
        raw.close()
    assert count == 1


@pytest.mark.asyncio
async def test_migration_bootstrap_pre_framework_db(tmp_path: pathlib.Path) -> None:
    """Pre-framework DB (no schema_migrations): bootstraps without re-running 0001."""
    db_path = str(tmp_path / "fleet.db")
    # Simulate a pre-framework DB: apply init SQL with raw sqlite3 only
    init_sql = (MIGRATIONS_DIR / "0001_init.sql").read_text()
    raw = _sqlite3.connect(db_path)
    raw.executescript(init_sql)
    raw.close()

    # Confirm no schema_migrations table yet
    raw = _sqlite3.connect(db_path)
    rows = raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in rows}
    raw.close()
    assert "schema_migrations" not in tables, "pre-condition: no schema_migrations yet"

    # init_db should bootstrap gracefully
    db = await init_db(db_path)
    await db.close()

    raw = _sqlite3.connect(db_path)
    try:
        versions = [r[0] for r in raw.execute("SELECT version FROM schema_migrations")]
    finally:
        raw.close()
    assert "0001_init" in versions


@pytest.mark.asyncio
async def test_migration_custom_dir_applies_pending(tmp_path: pathlib.Path) -> None:
    """A pending migration file in a custom migrations_dir is applied on init_db."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    # Minimal 0001 with agents table so bootstrap detection works
    (mdir / "0001_init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, name TEXT NOT NULL);\n"
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY);\n"
    )
    # Pending 0002
    (mdir / "0002_marker.sql").write_text(
        "CREATE TABLE migration_marker (id INTEGER PRIMARY KEY);\n"
    )

    db_path = str(tmp_path / "fleet.db")
    db = await init_db(db_path, migrations_dir=mdir)
    await db.close()

    raw = _sqlite3.connect(db_path)
    try:
        versions = [
            r[0] for r in raw.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        _tbl_rows = raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in _tbl_rows}
    finally:
        raw.close()

    assert "0001_init" in versions
    assert "0002_marker" in versions
    assert "migration_marker" in tables


@pytest.mark.asyncio
async def test_migration_trigger_with_semicolon_applies(tmp_path: pathlib.Path) -> None:
    """Migration containing a trigger (semicolons in body) applies without error."""
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, name TEXT NOT NULL);\n"
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY);\n"
    )
    (mdir / "0002_trigger.sql").write_text(
        "CREATE TRIGGER IF NOT EXISTS trg_test AFTER INSERT ON agents\n"
        "BEGIN\n"
        "  SELECT CASE WHEN 1=0 THEN 'semi;colon' ELSE 'ok' END;\n"
        "END;\n"
    )

    db_path = str(tmp_path / "fleet.db")
    db = await init_db(db_path, migrations_dir=mdir)
    await db.close()

    raw = _sqlite3.connect(db_path)
    try:
        triggers = {r[0] for r in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )}
        versions = [r[0] for r in raw.execute("SELECT version FROM schema_migrations")]
    finally:
        raw.close()

    assert "trg_test" in triggers
    assert "0002_trigger" in versions
