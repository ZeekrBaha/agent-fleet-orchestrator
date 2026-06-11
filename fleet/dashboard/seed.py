"""Seed helper for Playwright smoke tests.

Creates a deterministic dataset in a given SQLite DB file:
- 1 repository
- 6 agents covering all statuses
- 20 events of mixed types
- 2 worktrees
- 1 task with 3 evidence rows (2 pass, 1 skip)
- 1 pending approval + 1 decided approval
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from fleet.util.time import utcnow_iso


def seed_test_db(db_path: str) -> None:
    """Insert deterministic test data into *db_path* (must already have schema)."""
    conn = sqlite3.connect(db_path)
    try:
        _seed(conn)
        conn.commit()
    finally:
        conn.close()


def _now(offset_seconds: int = 0) -> str:
    if offset_seconds == 0:
        return utcnow_iso()
    ts = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    return ts.isoformat()


# Fixed timestamps for determinism
_TS_BASE = "2026-06-10T10:00:00.000000+00:00"
_TS_OLD = "2026-06-09T08:00:00.000000+00:00"
_TS_RECENT = "2026-06-10T09:55:00.000000+00:00"


def _seed(conn: sqlite3.Connection) -> None:
    # ---- repository ----
    conn.execute(
        """
        INSERT OR IGNORE INTO repositories
          (id, path, default_branch, merge_policy_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "repo-1",
            "/home/fleet/project",
            "main",
            '{"require_reviewer":true}',
            _TS_BASE,
        ),
    )

    # ---- agents — one per status ----
    agents = [
        (
            "agent-idle", "planner", "fleet-test", "orchestrator",
            "mock", "claude-sonnet-4-6",
            "idle", None, "repo-1", None, None, 12.5, 0.003, None, None,
        ),
        (
            "agent-running", "worker-a", "fleet-test", "worker",
            "mock", "claude-haiku-3",
            "running", "agent-idle", "repo-1", None, "wt-1", 67.3, 0.012, 1.0, 5.0,
        ),
        (
            "agent-waiting", "worker-b", "fleet-test", "worker",
            "mock", "claude-sonnet-4-6",
            "waiting", "agent-idle", "repo-1", None, None, 4.0, 0.001, 1.0, 5.0,
        ),
        (
            "agent-paused", "worker-c", "fleet-test", "worker",
            "mock", "claude-haiku-3",
            "paused_budget", "agent-idle", "repo-1", None, None, 90.0, 1.01, 1.0, 5.0,
        ),
        (
            "agent-failed", "worker-d", "fleet-test", "worker",
            "mock", "claude-haiku-3",
            "failed", "agent-idle", "repo-1", None, None, 55.0, 0.432, 1.0, 5.0,
        ),
        (
            "agent-archived", "worker-e", "fleet-test", "worker",
            "mock", "claude-haiku-3",
            "archived", "agent-idle", "repo-1", None, None, 100.0, 0.987, 1.0, 5.0,
        ),
    ]
    for a in agents:
        conn.execute(
            """
            INSERT OR IGNORE INTO agents
              (id, name, scope, role, backend, model, status, parent_id,
               repository_id, session_ref, worktree_id, context_pct, cost_usd,
               budget_soft_usd, budget_hard_usd, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*a, _TS_OLD, _TS_RECENT),
        )

    # ---- worktrees ----
    worktrees = [
        (
            "wt-1", "agent-running", "repo-1", "task-1",
            "/home/fleet/project/.git/worktrees/wt-1",
            "feat/worker-a-task", "main", '["src/"]', "active", _TS_OLD,
        ),
        (
            "wt-2", "agent-archived", "repo-1", None,
            "/home/fleet/project/.git/worktrees/wt-2",
            "feat/worker-e-old", "main", '[]', "removed", _TS_OLD,
        ),
    ]
    for wt in worktrees:
        conn.execute(
            """
            INSERT OR IGNORE INTO worktrees
              (id, agent_id, repository_id, task_id, path, branch, base_branch,
               owned_paths_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            wt,
        )

    # ---- task ----
    conn.execute(
        """
        INSERT OR IGNORE INTO tasks
          (id, scope, title, description, status, owner_agent_id, branch,
           acceptance_criteria_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-1", "fleet-test", "Implement login page",
            "Add a secure login page with JWT auth",
            "in_progress", "agent-running", "feat/worker-a-task",
            '["Login page renders","JWT token issued on success"]',
            _TS_OLD, _TS_RECENT,
        ),
    )

    # ---- validation evidence ----
    evidence = [
        (
            "task-1", "lint_check", "pass",
            "ruff check . — no issues found",
            "agent-running", _TS_RECENT,
        ),
        (
            "task-1", "unit_tests", "pass",
            "pytest -q — 42 passed in 3.2s",
            "agent-running", _TS_RECENT,
        ),
        (
            "task-1", "e2e_tests", "skip",
            "E2E tests skipped — browser not available in CI",
            "agent-running", _TS_RECENT,
        ),
    ]
    for ev in evidence:
        conn.execute(
            """
            INSERT OR IGNORE INTO validation_evidence
              (task_id, check_name, status, output, recorded_by, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ev,
        )

    # ---- approvals ----
    approvals = [
        (
            "appr-1", "fleet-test", "agent-running",
            "delete_branch feat/old-feature",
            "Branch is no longer needed — all work merged",
            "low", "pending", None, None, _TS_RECENT, None,
        ),
        (
            "appr-2", "fleet-test", "agent-running",
            "increase_budget 10.0",
            "Need extra budget to complete large refactor task",
            "medium", "approved", "human-operator", "LGTM", _TS_OLD, _TS_RECENT,
        ),
    ]
    for ap in approvals:
        conn.execute(
            """
            INSERT OR IGNORE INTO approvals
              (id, scope, requester_agent_id, operation, rationale, risk, status,
               decided_by, comment, created_at, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ap,
        )

    # ---- events (20 of mixed types) ----
    event_types = [
        "agent.started", "agent.idle", "task.created", "tool_call",
        "tool_result", "turn_end", "agent.message", "approval_request",
        "approval_decision", "error", "agent.started", "task.updated",
        "tool_call", "turn_end", "agent.message", "error", "agent.idle",
        "task.created", "tool_result", "turn_end",
    ]
    event_agents = [
        "agent-running", "agent-idle", "agent-running", "agent-running",
        "agent-running", "agent-running", "agent-idle", "agent-running",
        "agent-running", "agent-failed", "agent-running", "agent-running",
        "agent-running", "agent-running", "agent-idle", "agent-failed",
        "agent-waiting", "agent-running", "agent-running", "agent-running",
    ]
    summaries = [
        "Agent worker-a started session",
        "Agent planner entered idle state",
        "Task created: Implement login page",
        "Calling tool: read_file src/auth.py",
        "Tool result: 142 lines read",
        "Turn 1 complete — $0.004 — 2.1k tok — 3.2s",
        "Orchestrator sent instruction to worker-a",
        "Approval requested: delete_branch feat/old-feature",
        "Approval approved: increase_budget 10.0",
        "RuntimeError: Connection refused at localhost:5432",
        "Agent worker-a restarted after failure",
        "Task status updated to in_progress",
        "Calling tool: write_file src/login.py",
        "Turn 2 complete — $0.006 — 3.4k tok — 4.1s",
        "Orchestrator status update: 2 workers active",
        "ValueError: Invalid token format",
        "Agent worker-b waiting for approval",
        "Task created: Write unit tests",
        "Tool result: file written successfully",
        "Turn 3 complete — $0.003 — 1.8k tok — 2.9s",
    ]

    for i, (etype, agent, summary) in enumerate(
        zip(event_types, event_agents, summaries, strict=False)
    ):
        conn.execute(
            """
            INSERT INTO events (ts, scope, agent_id, type, summary, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _TS_RECENT, "fleet-test", agent, etype, summary,
                '{"seq":' + str(i) + '}',
            ),
        )
