"""Tests for fleet/cli.py — doctor and backup commands.

Written RED-first (TDD): tests confirmed failing before implementation.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _migration_sql() -> str:
    path = Path(__file__).parent.parent / "fleet" / "migrations" / "0001_init.sql"
    return path.read_text()


def _create_db(path: Path) -> None:
    """Create a test DB by executing the migration SQL."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_migration_sql())
    conn.commit()
    conn.close()


def _run_doctor(db_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "fleet.cli", "doctor", "--db", str(db_path)],
        capture_output=True,
        text=True,
    )


def _run_backup(db_path: Path, output_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "fleet.cli",
            "backup",
            "--db",
            str(db_path),
            "--output",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Doctor tests
# ---------------------------------------------------------------------------


class TestDoctorNoIssues:
    """Clean DB → exit 0, all [OK] lines."""

    def test_doctor_no_issues(self, tmp_path: Path) -> None:
        db = tmp_path / "fleet.db"
        _create_db(db)

        result = _run_doctor(db)

        assert result.returncode == 0
        assert "[OK]" in result.stdout
        assert "[WARN]" not in result.stdout


class TestDoctorOrphanWorktree:
    """Active worktree whose path does not exist on disk → exit 1, WARN."""

    def test_doctor_orphan_worktree(self, tmp_path: Path) -> None:
        db = tmp_path / "fleet.db"
        _create_db(db)

        # SQLite does not enforce FK constraints by default, so we insert
        # directly without parent rows.
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO worktrees"
            " (id, agent_id, repository_id, path,"
            "  branch, base_branch, status, created_at)"
            " VALUES ('wt1', 'agent1', 'repo1', '/nonexistent/path/to/wt',"
            "  'fleet/test', 'main', 'active', datetime('now'))"
        )
        conn.commit()
        conn.close()

        result = _run_doctor(db)

        assert result.returncode == 1
        assert "[WARN]" in result.stdout
        assert "Orphan" in result.stdout


class TestDoctorStaleInbox:
    """Pending inbox message older than 1 hour → exit 1, WARN."""

    def test_doctor_stale_inbox(self, tmp_path: Path) -> None:
        db = tmp_path / "fleet.db"
        _create_db(db)

        old_ts = (datetime.now(UTC) - timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO inbox (to_agent_id, sender, message, status, created_at)"
            f" VALUES ('agent1', 'system', 'hello', 'pending', '{old_ts}')"
        )
        conn.commit()
        conn.close()

        result = _run_doctor(db)

        assert result.returncode == 1
        assert "[WARN]" in result.stdout
        assert "Stale inbox" in result.stdout


class TestDoctorStuckAgent:
    """Agent with status='running' and updated_at > 10 minutes ago → exit 1, WARN."""

    def test_doctor_stuck_agent(self, tmp_path: Path) -> None:
        db = tmp_path / "fleet.db"
        _create_db(db)

        old_ts = (datetime.now(UTC) - timedelta(minutes=15)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO agents"
            " (id, name, scope, role, backend, model, status,"
            "  context_pct, cost_usd, created_at, updated_at)"
            f" VALUES ('agent1', 'agent1', 'test', 'coder', 'claude',"
            f"  'claude-3-5-sonnet-20241022', 'running',"
            f"  0.0, 0.0, '{old_ts}', '{old_ts}')"
        )
        conn.commit()
        conn.close()

        result = _run_doctor(db)

        assert result.returncode == 1
        assert "[WARN]" in result.stdout
        assert "Stuck" in result.stdout


# ---------------------------------------------------------------------------
# Backup tests
# ---------------------------------------------------------------------------


class TestBackupCreatesTarball:
    """backup command creates a .tar.gz containing backup_meta.json."""

    def test_backup_creates_tarball(self, tmp_path: Path) -> None:
        db = tmp_path / "fleet.db"
        _create_db(db)
        output_dir = tmp_path / "backups"
        output_dir.mkdir()

        result = _run_backup(db, output_dir)

        assert result.returncode == 0, result.stderr
        tarballs = list(output_dir.glob("fleet-backup-*.tar.gz"))
        assert len(tarballs) == 1, f"Expected 1 tarball, found: {tarballs}"

        with tarfile.open(tarballs[0]) as tf:
            names = tf.getnames()

        assert any("backup_meta.json" in n for n in names), (
            f"backup_meta.json not found in tarball. Contents: {names}"
        )
        assert any(db.name in n for n in names), (
            f"{db.name} not found in tarball. Contents: {names}"
        )
        assert any("fleet/manifests" in n for n in names), (
            f"fleet/manifests/ not found in tarball. Contents: {names}"
        )


class TestBackupMetaJson:
    """backup_meta.json inside tarball has ts, db_path, and version keys."""

    def test_backup_meta_json(self, tmp_path: Path) -> None:
        db = tmp_path / "fleet.db"
        _create_db(db)
        output_dir = tmp_path / "backups"
        output_dir.mkdir()

        result = _run_backup(db, output_dir)

        assert result.returncode == 0, result.stderr
        tarballs = list(output_dir.glob("fleet-backup-*.tar.gz"))
        assert len(tarballs) == 1

        with tarfile.open(tarballs[0]) as tf:
            meta_member = next(
                m for m in tf.getmembers() if "backup_meta.json" in m.name
            )
            raw = tf.extractfile(meta_member)
            assert raw is not None
            meta = json.loads(raw.read())

        assert "ts" in meta, f"Missing 'ts' key in meta: {meta}"
        assert "db_path" in meta, f"Missing 'db_path' key in meta: {meta}"
        assert "version" in meta, f"Missing 'version' key in meta: {meta}"
        assert meta["version"] == "1"
