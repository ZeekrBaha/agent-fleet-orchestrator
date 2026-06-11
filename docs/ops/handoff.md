# Fleet Build Handoff

Date: 2026-06-10
Current task: **Task 7.2 — Ops hardening** (NOT YET STARTED)

---

## Completed phases

| Phase | Tasks | Tests |
|-------|-------|-------|
| 1 — Scaffold | pyproject, config, models, db, migrations | — |
| 2 — Core infra | events, SSE, API auth, backends (mock/claude), inbox | — |
| 3 — Workspace & git | gitops, workspace service, worktree service | — |
| 4 — Policy & tools | policy rules/service, manifest, MCP toolserver, tool schemas/handlers | — |
| 5 — Agent runtime | prompt builder (5.1), spawn_worker + evidence (5.2), compaction + memory (5.3) | 152 |
| 6 — Merge & review | conflict + merge gate (6.1), approval queue (6.2), reviewer role (6.3) | 185 |
| 7.1 — Dashboard | 6 views (Jinja+htmx), SSE live tail, Playwright smoke (11 tests) | 185 non-slow + 11 Playwright |

**Current test count:** 185 passing (non-slow), 13 deselected (slow/live), 0 failing.
Lint: `ruff` clean. Types: `mypy` clean (50 source files).

---

## Pending

- **Task 7.2 — Ops hardening** (NOT started)
- Task 7.3 — Telegram bridge (post-MVP, skip)
- Spec compliance review for 7.2
- Code quality review for 7.2
- Final validation report (`docs/implementation/validation-report.md`)

---

## Task 7.2 implementer prompt (verbatim)

```
You are a senior developer implementing Task 7.2 of the Fleet project: ops hardening. Follow TDD.

## Project context

Working directory: `/Users/baha/Desktop/llm-ai-projects/agent-fleet-orchestrator`
Stack: Python + FastAPI + SQLite + asyncio + uv
Tests: `uv run pytest -q -m "not live and not slow"` (185 passing — keep all green)
Lint/type: `uv run ruff check .` + `uv run mypy fleet`

Before coding, read:
- `fleet/db.py` — DatabaseManager, init_db, read_connection
- `fleet/migrations/0001_init.sql` — all table schemas (worktrees, inbox, events, agents)
- `fleet/workspace/gitops.py` — git_run, worktree_remove
- `fleet/workspace/worktree_service.py` — WorktreeService
- `fleet/config.py` — Settings, get_settings

## Task: Ops hardening

### What to build

**1. `fleet doctor` CLI command**

File: `fleet/cli.py` (create if not exists)

`python -m fleet.cli doctor` (or `uv run fleet doctor`) runs a set of checks and prints a health report. Use `typer` (already a dependency or add it) or `argparse` (no new dep).

**Checks to implement:**

a) **Orphan worktrees** — worktree rows in DB with `status='active'` whose directory no longer exists on disk. Report: agent_id, branch, path.

b) **Stale inbox** — inbox rows with `status='pending'` older than 1 hour. Report: count + oldest message age.

c) **Unflushed events** — check that the event append queue is not backed up (approximate: if `SELECT COUNT(*) FROM events WHERE ts > datetime('now', '-1 minute')` returns >1000, warn about possible event storm).

d) **Agent stuck in running** — agents with `status='running'` and `updated_at` older than 2× the default turn timeout (600s). Likely zombie.

e) **Merge lock held** — (informational) if the in-process `MergeLock` dict is non-empty, report.

**Output format:**
```
Fleet doctor
============
[OK]   Orphan worktrees: 0
[WARN] Stale inbox: 3 messages, oldest 47m
[OK]   Event queue: normal
[WARN] Stuck agents: 1 agent (agent-xyz running for 15m)
[OK]   Merge lock: clear
```

Exit code 0 if all OK; exit code 1 if any WARN or ERROR.

**2. Backup**

File: `fleet/cli.py`

`fleet backup [--output DIR]` — creates a backup tarball:
- Copies the SQLite DB file
- Copies `fleet/manifests/` directory
- Writes a `backup_meta.json` with `{"ts": ..., "db_path": ..., "version": "1"}`
- Output: `fleet-backup-<ts>.tar.gz` in `--output` dir (default: current directory)

Use `shutil.copy2`, `tarfile`, `json` — no new dependencies.

**3. Restore documentation**

File: `docs/ops/restore.md` (create)

Brief markdown: how to restore from backup (stop server, replace DB file, restart); caveat that in-flight agent sessions are lost but persisted state resumes.

**4. Deployment documentation**

File: `docs/ops/deploy.md` (create)

Brief markdown: how to run Fleet in production with uvicorn + systemd. Include:
- `uv run uvicorn fleet.main:app --host 0.0.0.0 --port 8000`
- systemd unit file template
- Required env vars: `FLEET_API_TOKEN`, `FLEET_DB_PATH`, `FLEET_HOST`, `FLEET_PORT`
- Security note: set `FLEET_API_TOKEN` to a long random value; bind to localhost behind nginx in production

**5. Wire everything**

File: `fleet/main.py` (create if not exists — or check if it exists)

Ensure there's a `fleet/main.py` that creates the FastAPI `app` and includes all routers:
- `fleet.api.agents` (if exists)
- `fleet.api.events`
- `fleet.api.tools`
- `fleet.api.approvals`
- `fleet.api.merge`
- `fleet.api.review`
- `fleet.dashboard.router`

If `fleet/main.py` doesn't exist yet, create it. If it exists, check it includes the dashboard router and all other routers.

### Files to create/modify

- Create: `fleet/cli.py`
- Create: `docs/ops/restore.md`
- Create: `docs/ops/deploy.md`
- Create or modify: `fleet/main.py`
- Create: `tests/test_cli.py`

### TDD — write tests FIRST in `tests/test_cli.py`

1. `test_doctor_no_issues` — clean DB (no orphans, no stale, no stuck) → exit code 0, all OK lines
2. `test_doctor_orphan_worktree` — DB has worktree row with non-existent path → exit code 1, WARN in output
3. `test_doctor_stale_inbox` — DB has inbox row `created_at` > 1 hour ago with `status='pending'` → exit code 1, WARN
4. `test_doctor_stuck_agent` — DB has agent `status='running'` with `updated_at` > 10 minutes ago → exit code 1, WARN
5. `test_backup_creates_tarball` — `fleet backup --output <tmpdir>` creates a `.tar.gz` containing the DB and manifest files + `backup_meta.json`
6. `test_backup_meta_json` — `backup_meta.json` inside tarball has `ts`, `db_path`, `version` keys

### Constraints
- `except Exception` only with typed mapping + comment
- No TODO comments
- CLI module ≤ 300 lines
- No new dependencies beyond what's already in pyproject.toml; use stdlib for backup (tarfile, shutil, json)
- Doctor checks run against the DB file path from `Settings.db_path`; accept `--db` override flag

### Verification
```
uv run ruff check .
uv run mypy fleet
uv run pytest -q -m "not live and not slow"
```

Report: files changed, each test red→green, final counts, deviations. Status: DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED.
```

---

## Steps after Task 7.2 implementer completes

### Step 1 — Spec compliance review

Dispatch a spec compliance reviewer subagent with:

**Files to read:** `fleet/cli.py`, `tests/test_cli.py`, `docs/ops/restore.md`, `docs/ops/deploy.md`, `fleet/main.py`

**Checks:**
- `fleet doctor` implements all 5 checks (orphan worktrees, stale inbox, event queue, stuck agents, merge lock)
- Output format matches exactly (OK/WARN/ERROR prefix, exit 0/1)
- `fleet backup` produces `.tar.gz` with DB + manifests + `backup_meta.json`
- `backup_meta.json` has `ts`, `db_path`, `version`
- `fleet/main.py` includes all 7 routers
- `docs/ops/restore.md` and `docs/ops/deploy.md` exist and contain systemd template + required env vars
- All 6 TDD tests exist and cover the required scenarios

### Step 2 — Code quality review

Dispatch a code quality reviewer (`caveman:cavecrew-reviewer`) with:

**Files:** `fleet/cli.py`, `fleet/main.py`, `tests/test_cli.py`

**Constraints to check:**
- No `except Exception` without typed mapping + comment
- CLI module ≤ 300 lines
- No blocking subprocess calls in async context (doctor runs sync — that's fine if called from sync CLI entry point)
- No TODO comments
- Proper exit codes (sys.exit vs raise SystemExit)

### Step 3 — Final validation report

After both reviews pass, write `docs/implementation/validation-report.md` with:

```markdown
# Validation Report

Date: <today>
Build: Fleet MVP
Test suite: <N> passed, 0 failing, <M> deselected (slow/live)
Playwright: 11 passed

## Phase gates

| Phase | Gate | Status | Evidence |
|-------|------|--------|----------|
| 1 | Scaffold compiles, config loads | PASS | mypy clean |
| 2 | Events append + SSE stream + auth | PASS | test_events.py |
| 3 | Worktree create/remove + dirty-repo guard | PASS | test_workspace.py |
| 4 | Policy deny + tool audit events | PASS | test_policy.py, test_tools.py |
| 5 | MockBackend orchestration golden test (AC-020) | PASS | test_orchestration.py |
| 6 | Merge gate + approval round-trip (AC-013, AC-032, AC-033) | PASS | test_merge_gate.py, test_approvals.py |
| 7 | Dashboard all-states smoke (AC-050) | PASS | test_dashboard_smoke.py |

## Acceptance criteria status
<one line per AC from requirements.md — status PASS/FAIL/PARTIAL>

## Risk register
<any known residual risks>

## Skipped / deferred
- Task 7.3: Telegram bridge (post-MVP)
- Live backend tests (require ANTHROPIC_API_KEY)
```

---

## Quick resume commands

```bash
cd /Users/baha/Desktop/llm-ai-projects/agent-fleet-orchestrator

# Verify current state
uv run pytest -q -m "not live and not slow"
uv run ruff check . && uv run mypy fleet

# Run Playwright smoke
uv run pytest tests/test_dashboard_smoke.py -m slow

# Start server (once main.py is complete)
FLEET_API_TOKEN=<token> uv run uvicorn fleet.main:app --reload
```
