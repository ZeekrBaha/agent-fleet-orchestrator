"""P0-3: Evidence commit SHA binding — staleness detection.

Without commit SHA binding, an agent can record green evidence on commit X,
push commit Y, and merge Y under X's passing evidence — a full gate bypass.

Tests (TDD-first):
  1. record_evidence stores the provided commit_sha.
  2. check_merge_gate rejects when evidence SHA != current branch SHA.
  3. check_merge_gate accepts when evidence SHA == current branch SHA.
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from fleet.db import DatabaseManager, init_db
from fleet.review.evidence import EvidenceService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db(tmp_path: Path) -> Any:
    db_path = str(tmp_path / "test.db")
    manager = await init_db(db_path)
    yield manager
    await manager.close()


@pytest_asyncio.fixture()
async def evidence_svc(db: DatabaseManager) -> Any:
    return EvidenceService(db, gate_require_reviewer=False)


def _make_git_repo(path: Path) -> None:
    """Create a minimal git repo with one commit and return HEAD SHA."""
    def _git(args: list[str]) -> str:
        result = subprocess.run(
            ["git"] + args, cwd=path, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    _git(["init", "-b", "main"])
    _git(["config", "user.email", "test@fleet.local"])
    _git(["config", "user.name", "Fleet Test"])
    (path / "README.md").write_text("# test\n")
    _git(["add", "README.md"])
    _git(["commit", "-m", "initial"])


def _git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _git_commit(path: Path, msg: str = "extra commit") -> str:
    def _git(args: list[str]) -> str:
        result = subprocess.run(
            ["git"] + args, cwd=path, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    (path / "extra.txt").write_text(f"{msg}\n")
    _git(["add", "extra.txt"])
    _git(["commit", "-m", msg])
    return _git(["rev-parse", "HEAD"])


async def _make_task(evidence_svc: EvidenceService) -> str:
    return await evidence_svc.create_task(
        scope="test",
        title="T1",
        description="desc",
        owner_agent_id="agent-1",
        branch="main",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_records_commit_sha(
    db: DatabaseManager, evidence_svc: EvidenceService, tmp_path: Path
) -> None:
    """record_evidence stores the provided commit_sha in the DB row."""
    worktree_path = tmp_path / "repo"
    worktree_path.mkdir()
    _make_git_repo(worktree_path)
    sha = _git_head(worktree_path)

    task_id = await _make_task(evidence_svc)
    await evidence_svc.record_evidence(
        task_id=task_id,
        check_name="pytest",
        status="pass",
        commit_sha=sha,
    )

    with db.read_connection() as conn:
        row = conn.execute(
            text("SELECT commit_sha FROM validation_evidence WHERE task_id = :tid"),
            {"tid": task_id},
        ).fetchone()

    assert row is not None
    assert row[0] == sha, f"Expected {sha!r}, got {row[0]!r}"


@pytest.mark.asyncio
async def test_merge_rejects_stale_evidence(
    evidence_svc: EvidenceService, tmp_path: Path
) -> None:
    """Evidence recorded at SHA-X must be rejected if branch now points to SHA-Y."""
    worktree_path = tmp_path / "repo"
    worktree_path.mkdir()
    _make_git_repo(worktree_path)
    sha_at_record = _git_head(worktree_path)

    task_id = await _make_task(evidence_svc)
    await evidence_svc.record_evidence(
        task_id=task_id,
        check_name="pytest",
        status="pass",
        commit_sha=sha_at_record,
    )

    # Push a new commit — evidence is now stale.
    sha_new = _git_commit(worktree_path, "new commit after evidence recorded")

    can_merge, reason = await evidence_svc.check_merge_gate(
        task_id, branch_sha=sha_new
    )
    assert not can_merge, "Stale evidence should block merge"
    assert "stale" in reason.lower() or "sha" in reason.lower(), (
        f"Unexpected reason: {reason!r}"
    )


@pytest.mark.asyncio
async def test_merge_accepts_fresh_evidence(
    evidence_svc: EvidenceService, tmp_path: Path
) -> None:
    """Evidence SHA matching the current branch tip should allow merge."""
    worktree_path = tmp_path / "repo"
    worktree_path.mkdir()
    _make_git_repo(worktree_path)
    sha = _git_head(worktree_path)

    task_id = await _make_task(evidence_svc)
    await evidence_svc.record_evidence(
        task_id=task_id,
        check_name="pytest",
        status="pass",
        commit_sha=sha,
    )

    can_merge, reason = await evidence_svc.check_merge_gate(
        task_id, branch_sha=sha
    )
    assert can_merge, f"Fresh evidence should allow merge; reason: {reason!r}"
