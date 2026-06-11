"""B2: Schema + timestamp unification tests.

TDD RED phase — all tests must fail before the fix.

Behaviors tested:
1. fleet.util.time.utcnow_iso() importable and returns +00:00 format.
2. No Z-suffix format calls remain in fleet/ production sources.
3. validation_evidence.task_id schema is TEXT after migration.
4. Required indexes exist after migration.
5. Z-suffix timestamps in events/memory normalized to +00:00 by migration.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

FLEET_ROOT = Path(__file__).parent.parent / "fleet"


# ---------------------------------------------------------------------------
# 1. utcnow_iso() canonical function
# ---------------------------------------------------------------------------


def test_utcnow_iso_importable() -> None:
    """fleet.util.time.utcnow_iso must exist and be importable."""
    from fleet.util.time import utcnow_iso  # noqa: F401


def test_utcnow_iso_returns_plus00_format() -> None:
    """utcnow_iso() must return a string ending in '+00:00', not 'Z'."""
    from fleet.util.time import utcnow_iso

    ts = utcnow_iso()
    assert ts.endswith("+00:00"), (
        f"Expected +00:00 suffix, got: {ts!r}. "
        "All timestamps must use the canonical +00:00 format."
    )
    # Must be parseable as ISO 8601
    from datetime import datetime
    datetime.fromisoformat(ts)  # raises ValueError if malformed


# ---------------------------------------------------------------------------
# 2. No Z-suffix format calls in production sources
# ---------------------------------------------------------------------------


def test_no_z_suffix_format_in_fleet_sources() -> None:
    """No fleet/ production file may call .replace('+00:00', 'Z').

    After B2, all Z-format callers must be replaced with utcnow_iso().
    The only allowed location is fleet/util/time.py (if needed for compat).
    """
    violations: list[str] = []
    for py_file in FLEET_ROOT.rglob("*.py"):
        # util/time.py is the canonical owner; allow it there if needed
        if py_file.name == "time.py" and "util" in str(py_file):
            continue
        src = py_file.read_text(encoding="utf-8")
        if '.replace("+00:00", "Z")' in src or ".replace('+00:00', 'Z')" in src:
            violations.append(str(py_file.relative_to(FLEET_ROOT.parent)))
    assert not violations, (
        "Z-suffix format calls found; replace with utcnow_iso():\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 3 + 4. Schema after migration
# ---------------------------------------------------------------------------


@pytest.fixture()
def migrated_db(tmp_path: Any) -> sqlite3.Connection:
    """Return a sqlite3 connection to a fully-migrated DB."""
    import asyncio

    from fleet.db import init_db

    db_path = str(tmp_path / "b2_test.db")
    asyncio.run(init_db(db_path))
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()


def test_validation_evidence_task_id_is_text(migrated_db: sqlite3.Connection) -> None:
    """validation_evidence.task_id must be declared TEXT after migration.

    The original schema declared it INTEGER which is wrong — tasks.id is UUID text.
    """
    rows = migrated_db.execute(
        "PRAGMA table_info(validation_evidence)"
    ).fetchall()
    col_map = {row[1]: row[2].upper() for row in rows}  # name -> type
    assert "task_id" in col_map, "task_id column not found in validation_evidence"
    assert col_map["task_id"] == "TEXT", (
        f"task_id should be TEXT, got {col_map['task_id']!r}. "
        "INTEGER breaks UUID foreign key semantics."
    )


def test_indexes_created_by_migration(migrated_db: sqlite3.Connection) -> None:
    """Both performance indexes must exist after migration."""
    indexes = {
        row[1]
        for row in migrated_db.execute(
            "SELECT type, name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_inbox_to_status" in indexes, (
        "Missing index idx_inbox_to_status on inbox(to_agent_id, status)"
    )
    assert "idx_events_agent_id" in indexes, (
        "Missing index idx_events_agent_id on events(agent_id, id)"
    )


# ---------------------------------------------------------------------------
# 5. Z-suffix normalization in migration
# ---------------------------------------------------------------------------


def test_z_timestamps_normalized_by_migration(tmp_path: Any) -> None:
    """Existing Z-suffix timestamps in events and agent_memories must be
    rewritten to +00:00 format by migration 0004.

    Steps:
      1. Create a DB from 0001_init.sql only (pre-normalization state).
      2. Insert rows with Z-suffix timestamps.
      3. Apply all migrations.
      4. Verify the timestamps now have +00:00 suffix.
    """
    from fleet.db import MIGRATIONS_DIR, run_migrations

    db_path = str(tmp_path / "z_norm_test.db")

    # Apply only 0001 to get bare schema
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA foreign_keys=ON")
    init_sql = (MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
    raw.executescript(init_sql)
    raw.commit()

    # Seed Z-suffix timestamps
    raw.execute(
        "INSERT INTO events (ts, scope, agent_id, type, summary, payload_json)"
        " VALUES ('2026-06-01T12:00:00Z', 'test', NULL, 'test', 'test', '{}')"
    )
    raw.execute(
        "INSERT INTO agent_memories"
        " (agent_id, scope, kind, content, metadata_json, ts)"
        " VALUES ('agent-1', 'test', 'note', 'hello', '{}', '2026-06-01T12:00:00Z')"
    )
    raw.commit()
    raw.close()

    # Apply all migrations (including 0004 which normalizes Z → +00:00)
    run_migrations(db_path)

    # Verify normalization
    conn = sqlite3.connect(db_path)
    ev_ts = conn.execute("SELECT ts FROM events WHERE scope='test'").fetchone()[0]
    mem_ts = conn.execute(
        "SELECT ts FROM agent_memories WHERE agent_id='agent-1'"
    ).fetchone()[0]
    conn.close()

    assert ev_ts.endswith("+00:00"), (
        f"events.ts not normalized: {ev_ts!r} — expected +00:00 suffix"
    )
    assert mem_ts.endswith("+00:00"), (
        f"agent_memories.ts not normalized: {mem_ts!r} — expected +00:00 suffix"
    )
