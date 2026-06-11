"""Fleet ops CLI: doctor (health checks) and backup."""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime

from fleet.config import Settings


@dataclass
class _CheckResult:
    """Holds the outcome of a single doctor check."""

    level: str  # "OK" or "WARN"
    label: str
    message: str



def _check_orphan_worktrees(conn: sqlite3.Connection) -> _CheckResult:
    """Check (a): active worktree rows whose path no longer exists on disk."""
    rows = conn.execute(
        "SELECT agent_id, branch, path FROM worktrees WHERE status = 'active'"
    ).fetchall()
    orphans = [r for r in rows if not pathlib.Path(str(r[2])).exists()]
    if not orphans:
        return _CheckResult("OK", "Orphan worktrees", "0")
    detail = "; ".join(f"{r[0]} ({r[1]}) at {r[2]}" for r in orphans)
    return _CheckResult(
        "WARN",
        "Orphan worktrees",
        f"{len(orphans)} orphan(s): {detail}",
    )


def _check_stale_inbox(conn: sqlite3.Connection) -> _CheckResult:
    """Check (b): pending inbox messages older than 1 hour."""
    row = conn.execute(
        "SELECT COUNT(*), MIN(created_at)"
        " FROM inbox"
        " WHERE status = 'pending'"
        "   AND created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 hour')"
    ).fetchone()
    count: int = row[0]
    if count == 0:
        return _CheckResult("OK", "Stale inbox", "no stale messages")
    oldest_ts: str | None = row[1]
    age_str = _format_age(oldest_ts)
    return _CheckResult("WARN", "Stale inbox", f"{count} messages, oldest {age_str}")


def _check_event_storm(conn: sqlite3.Connection) -> _CheckResult:
    """Check (c): >1000 events in the last minute suggests an event storm."""
    row = conn.execute(
        "SELECT COUNT(*) FROM events"
        " WHERE ts > strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 minute')"
    ).fetchone()
    count: int = row[0]
    if count <= 1000:
        return _CheckResult("OK", "Event queue", "normal")
    return _CheckResult(
        "WARN",
        "Event queue",
        f"possible event storm: {count} events in last minute",
    )


def _check_stuck_agents(conn: sqlite3.Connection) -> _CheckResult:
    """Check (d): agents running longer than 600 seconds (2× default timeout)."""
    rows = conn.execute(
        "SELECT id, updated_at"
        " FROM agents"
        " WHERE status = 'running'"
        "   AND updated_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-600 seconds')"
    ).fetchall()
    if not rows:
        return _CheckResult("OK", "Stuck agents", "none")
    parts = []
    for row in rows:
        agent_id: str = str(row[0])
        updated_at: str | None = row[1]
        age_str = _format_age(updated_at)
        parts.append(f"{agent_id} running for {age_str}")
    return _CheckResult(
        "WARN",
        "Stuck agents",
        f"{len(rows)} agent(s): {'; '.join(parts)}",
    )


def _check_merge_lock() -> _CheckResult:
    """Check (e): whether any merge scope lock is held.

    MergeLock is process-local (asyncio in-memory dict).  When invoked
    from the CLI — a separate process — the dict is always empty.  We
    report OK with a clarifying note rather than a false WARN.
    """
    try:
        from fleet.review.lock import MergeLock  # noqa: PLC0415

        lock = MergeLock()
        held = [scope for scope, lk in lock._locks.items() if lk.locked()]
        if held:
            return _CheckResult(
                "WARN",
                "Merge lock",
                f"held for scope(s): {', '.join(held)}",
            )
        return _CheckResult(
            "OK",
            "Merge lock",
            "clear (process-local, always clear in CLI context)",
        )
    except ImportError as exc:  # maps ImportError → graceful note
        return _CheckResult(
            "OK",
            "Merge lock",
            f"(import failed: {exc}; always clear in CLI context)",
        )



def _format_age(ts: str | None) -> str:
    """Return a human-readable age string like '2h15m' for an ISO timestamp."""
    if not ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - dt
        total_min = int(delta.total_seconds() / 60)
        if total_min >= 60:
            return f"{total_min // 60}h{total_min % 60}m"
        return f"{total_min}m"
    except (ValueError, OverflowError) as exc:  # maps parse/overflow errors → unknown
        _ = exc  # acknowledged
        return "unknown"



def cmd_doctor(db_path: str) -> int:
    """Run all doctor checks; return exit code (0=OK, 1=WARN/ERROR)."""
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.OperationalError as exc:  # maps OperationalError → error message
        print(f"ERROR: cannot open database {db_path!r}: {exc}", file=sys.stderr)
        return 1

    try:
        checks = [
            _check_orphan_worktrees(conn),
            _check_stale_inbox(conn),
            _check_event_storm(conn),
            _check_stuck_agents(conn),
            _check_merge_lock(),
        ]
    finally:
        conn.close()

    print("Fleet doctor")
    print("============")
    for check in checks:
        # Align label: "[OK]   " and "[WARN] " are both 7 chars before the label.
        level_str = f"[{check.level}]"
        padding = " " * max(1, 7 - len(level_str))
        print(f"{level_str}{padding}{check.label}: {check.message}")

    has_issue = any(c.level in ("WARN", "ERROR") for c in checks)
    return 1 if has_issue else 0


def cmd_backup(db_path: str, output_dir: str) -> int:
    """Create a backup tarball of the DB and manifests directory."""
    db_file = pathlib.Path(db_path)
    if not db_file.exists():
        print(f"ERROR: database file not found: {db_path!r}", file=sys.stderr)
        return 1

    fleet_pkg = pathlib.Path(__file__).parent
    manifests_dir = fleet_pkg / "manifests"

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    tarball_name = f"fleet-backup-{ts}.tar.gz"
    out_path = pathlib.Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    tarball_path = out_path / tarball_name

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = pathlib.Path(tmp_str)

        # Copy the SQLite DB file.
        shutil.copy2(db_file, tmp / db_file.name)

        # Copy the manifests directory if it exists.
        if manifests_dir.exists():
            shutil.copytree(str(manifests_dir), str(tmp / "manifests"))

        # Write the backup metadata.
        meta: dict[str, str] = {
            "ts": datetime.now(UTC).isoformat(),
            "db_path": str(db_file.resolve()),
            "version": "1",
        }
        (tmp / "backup_meta.json").write_text(json.dumps(meta, indent=2))

        # Bundle everything into a .tar.gz.
        with tarfile.open(tarball_path, "w:gz") as tf:
            for item in sorted(tmp.iterdir()):
                arcname = (
                    f"fleet/{item.name}" if item.name == "manifests" else item.name
                )
                tf.add(item, arcname=arcname)

    print(f"Backup created: {tarball_path}")
    return 0



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m fleet.cli",
        description="Fleet ops CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run health checks and print a report.",
    )
    doctor_parser.add_argument(
        "--db",
        metavar="PATH",
        help="Override the DB path from FLEET_DB_PATH / settings.",
    )

    backup_parser = subparsers.add_parser(
        "backup",
        help="Create a backup tarball of the DB and manifests.",
    )
    backup_parser.add_argument(
        "--db",
        metavar="PATH",
        help="Override the DB path from FLEET_DB_PATH / settings.",
    )
    backup_parser.add_argument(
        "--output",
        metavar="DIR",
        default=".",
        help="Directory to write the tarball (default: current directory).",
    )

    return parser


def main() -> None:
    """Entry point for `python -m fleet.cli`."""
    parser = _build_parser()
    args = parser.parse_args()

    settings = Settings()
    db_path: str = args.db if args.db else settings.db_path

    if args.command == "doctor":
        sys.exit(cmd_doctor(db_path))
    elif args.command == "backup":
        output_dir: str = args.output
        sys.exit(cmd_backup(db_path, output_dir))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
