"""Tests for P1-24: WAL-safe backup using sqlite3.backup() API.

Written RED-first (TDD): tests must fail before the fix is applied.
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path
from unittest.mock import patch

from fleet.cli import cmd_backup

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


def _extract_backup_db(tarball: Path, dest_dir: Path) -> Path:
    """Extract the .db file from the backup tarball and return its path."""
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf.getmembers():
            if member.name.endswith(".db"):
                tf.extract(member, dest_dir, filter="data")
                # member.name may be just the filename (no directory prefix)
                return dest_dir / Path(member.name).name
    raise FileNotFoundError(f"No .db file found in {tarball}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBackupIntegrity:
    """P1-24: cmd_backup must produce a valid, readable SQLite database."""

    def test_backup_passes_quick_check(self, tmp_path: Path) -> None:
        """Backup DB must pass PRAGMA quick_check."""
        db = tmp_path / "fleet.db"
        _create_db(db)

        out_dir = tmp_path / "backups"
        rc = cmd_backup(str(db), str(out_dir))
        assert rc == 0

        tarballs = list(out_dir.glob("fleet-backup-*.tar.gz"))
        assert len(tarballs) == 1, "Expected exactly one tarball"

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        backup_db = _extract_backup_db(tarballs[0], extract_dir)

        check_conn = sqlite3.connect(str(backup_db))
        try:
            result = check_conn.execute("PRAGMA quick_check").fetchone()
        finally:
            check_conn.close()

        assert result is not None
        assert result[0] == "ok", f"PRAGMA quick_check returned: {result[0]!r}"

    def test_backup_uses_sqlite3_backup_not_shutil_copy2(self, tmp_path: Path) -> None:
        """cmd_backup must NOT use shutil.copy2 to copy the DB file.

        shutil.copy2 misses pages in the WAL file; sqlite3.Connection.backup()
        performs a safe hot online backup that reads committed WAL pages.

        Verification strategy:
        - Assert shutil.copy2 is never called with a .db file argument.
        - Assert sqlite3.connect is called (at least twice: src + dst connections).
        """
        db = tmp_path / "fleet.db"
        _create_db(db)

        out_dir = tmp_path / "backups"

        # Track calls to shutil.copy2 for .db files.
        copy2_db_calls: list[tuple[str, str]] = []

        import shutil as _shutil

        original_copy2 = _shutil.copy2

        def _spy_copy2(src, dst, **kwargs):  # type: ignore[override]
            if str(src).endswith(".db"):
                copy2_db_calls.append((str(src), str(dst)))
            return original_copy2(src, dst, **kwargs)

        # Track calls to sqlite3.connect.
        sqlite3_connect_calls: list[str] = []
        real_connect = sqlite3.connect

        def _spy_connect(path: str, *args, **kwargs):  # type: ignore[override]
            sqlite3_connect_calls.append(str(path))
            return real_connect(path, *args, **kwargs)

        with (
            patch("fleet.cli.shutil.copy2", side_effect=_spy_copy2),
            patch("fleet.cli.sqlite3.connect", side_effect=_spy_connect),
        ):
            rc = cmd_backup(str(db), str(out_dir))

        assert rc == 0, "cmd_backup should succeed"
        assert len(copy2_db_calls) == 0, (
            f"shutil.copy2 was called for a .db file: {copy2_db_calls} — "
            "use sqlite3.Connection.backup() instead"
        )
        # At minimum: one connect for source + one for dest + one for integrity check
        db_connects = [p for p in sqlite3_connect_calls if p.endswith(".db")]
        assert len(db_connects) >= 2, (
            f"Expected sqlite3.connect called at least twice for .db files, "
            f"got: {db_connects}"
        )

    def test_backup_captures_wal_mode_data(self, tmp_path: Path) -> None:
        """Data written while WAL mode is active must appear in the backup.

        This is the functional correctness test for P1-24: the backup must
        include all committed data regardless of WAL checkpoint state.
        """
        db = tmp_path / "fleet.db"
        _create_db(db)

        # Enable WAL mode explicitly and write a row.
        src_conn = sqlite3.connect(str(db))
        src_conn.execute("PRAGMA journal_mode=WAL")
        src_conn.execute(
            "INSERT INTO agents"
            " (id, name, scope, role, backend, model, status,"
            "  context_pct, cost_usd, created_at, updated_at)"
            " VALUES ('agt-wal', 'wal-agent', 'test', 'coder', 'mock', 'mock',"
            "  'idle', 0.0, 0.0, '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"
        )
        src_conn.commit()
        src_conn.close()

        out_dir = tmp_path / "backups"
        rc = cmd_backup(str(db), str(out_dir))
        assert rc == 0

        tarballs = list(out_dir.glob("fleet-backup-*.tar.gz"))
        assert len(tarballs) == 1

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        backup_db = _extract_backup_db(tarballs[0], extract_dir)

        check_conn = sqlite3.connect(str(backup_db))
        try:
            rows = check_conn.execute(
                "SELECT id FROM agents WHERE id = 'agt-wal'"
            ).fetchall()
        finally:
            check_conn.close()

        assert len(rows) == 1, (
            "WAL-mode data not found in backup — backup may not be reading WAL pages"
        )

    def test_backup_does_not_corrupt_source(self, tmp_path: Path) -> None:
        """The source DB must still contain all data after backup completes."""
        db = tmp_path / "fleet.db"
        _create_db(db)

        # Write two rows to source before backup.
        src_conn = sqlite3.connect(str(db))
        src_conn.execute("PRAGMA journal_mode=WAL")
        for i in range(2):
            src_conn.execute(
                "INSERT INTO agents"
                " (id, name, scope, role, backend, model, status,"
                "  context_pct, cost_usd, created_at, updated_at)"
                f" VALUES ('agt-{i}', 'agent-{i}', 'test', 'coder', 'mock', 'mock',"
                "  'idle', 0.0, 0.0, '2024-01-01T00:00:00Z', '2024-01-01T00:00:00Z')"
            )
        src_conn.commit()
        src_conn.close()

        out_dir = tmp_path / "backups"
        rc = cmd_backup(str(db), str(out_dir))
        assert rc == 0

        # Source DB must still be readable and contain both rows.
        verify_conn = sqlite3.connect(str(db))
        try:
            rows = verify_conn.execute("SELECT id FROM agents ORDER BY id").fetchall()
            check = verify_conn.execute("PRAGMA quick_check").fetchone()
        finally:
            verify_conn.close()

        assert check is not None and check[0] == "ok", (
            f"Source DB corrupted after backup: {check}"
        )
        ids = [r[0] for r in rows]
        assert "agt-0" in ids and "agt-1" in ids, (
            f"Source DB missing rows after backup: {ids}"
        )
