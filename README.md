# Agent Fleet Orchestrator

> **One sentence:** a self-contained Python server that spawns, supervises, and merges AI coding agents — each in its own git worktree — with policy enforcement, evidence-gated merges, budget controls, an approval queue, and a live web dashboard.

> **Status (measured, offline):** MVP complete and verified — `uv run pytest -q -m "not live and not slow"` is green (**191 tests pass**), `ruff` clean, `mypy` clean across 52 source files, 11 Playwright dashboard smoke tests pass.
>
> **Implemented:** agent lifecycle (spawn → turn → compact → hibernate → merge) · git worktree isolation · dirty-repo guard · inbox messaging with restart recovery · SSE event stream · MCP tool server (out-of-process) · role-manifest policy (fail-closed) · evidence-gated squash merge · approval queue · per-agent USD budget · context compaction with typed project memory · reviewer role · web dashboard (6 views, htmx, live SSE tail) · ops CLI (`fleet doctor` + `fleet backup`) · production deployment guide.
>
> **Not yet wired (post-MVP):** Telegram bridge (Task 7.3); live Claude backend tests (require `ANTHROPIC_API_KEY`; covered offline by `MockBackend`).

If reading cold, start with **§1 Mental model** and **§3 Quick start**.

---

## 1. Mental model

```
User / Orchestrator agent
        │
        │  POST /api/agents              spawn orchestrator or worker
        │  POST /api/agents/{id}/message  send a task or reply
        │  GET  /api/agents/{id}/events   SSE stream of live events
        ▼
  ┌─────────────────────────────────────────────────┐
  │  Fleet server  (FastAPI + asyncio + SQLite WAL) │
  │                                                 │
  │  Agent Service ──► Backend (Claude / Mock)      │
  │      │   turns, inbox, compaction, budgets      │
  │      │                                          │
  │  Workspace Service ──► git worktrees on disk    │
  │      │   branch-per-worker, dirty-repo guard    │
  │      │                                          │
  │  Policy Service ──► role manifests (YAML)       │
  │      │   fail-closed capability checks          │
  │      │                                          │
  │  Review / Merge Service                         │
  │      │   conflict sim · evidence gate · squash  │
  │      │                                          │
  │  Approval Queue ──► human-in-the-loop           │
  │      │   blocks merges, costly tools, budgets   │
  │      │                                          │
  │  Event Log ──► SSE hub ──► Dashboard            │
  │      append-only, typed, retained window        │
  └─────────────────────────────────────────────────┘
        │
        │  stdio (MCP protocol)
        ▼
  MCP Tool Server  (separate process, token-authenticated)
        │   tools: spawn_worker · send_message · read_file
        │          run_tests · request_merge · ...
        ▼
  Agent turn (Claude or MockBackend)
```

Fleet treats the event log as the source of truth. Every mutation emits an event first; agent table rows are derived state (ADR-004). A crash mid-turn means the next server start replays the log and restores every agent to its last durable state.

---

## 2. Why it exists

Existing agentic coding tools either hand the model direct filesystem access or run everything in one process. Fleet's bets:

| Concern | Fleet's answer |
|---------|----------------|
| Concurrent agents trample each other | One **git worktree per worker**, ownership paths enforced at spawn (no overlapping globs) |
| Agents make unreviewed commits | **Evidence-gated squash merge** — no merge without test results, reviewer verdict, and human approval |
| Runaway token/dollar costs | **Per-agent hard and soft USD budgets**; hard limit pauses the agent and opens an approval request |
| Policy enforcement is ad-hoc | **Fail-closed role manifests** — unknown tool or unknown role → deny + audit event, never silent pass |
| Context windows eventually fill | **Auto-compaction** at 80% usage: session summarised, typed facts written to project memory, fresh session resumes with the summary |
| Hard to observe what agents are doing | **Append-only event stream** over SSE; dashboard shows roster, timeline, conversations, diffs, approval queue |

---

## 3. Quick start

### Prerequisites

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) (package manager)
- Git

### Install and run (offline / MockBackend)

```bash
git clone https://github.com/ZeekrBaha/agent-fleet-orchestrator
cd agent-fleet-orchestrator
uv sync

# Run the test suite (no API key required — MockBackend replays fixtures)
uv run pytest -q -m "not live and not slow"
# 191 passed, 13 deselected

# Start the server with a random token
FLEET_API_TOKEN=dev-token uv run uvicorn fleet.main:app --reload
# Dashboard: http://localhost:8000/dashboard
```

### Run with Claude (live backend)

```bash
export FLEET_API_TOKEN=<your-token>
export ANTHROPIC_API_KEY=<your-anthropic-key>
uv run uvicorn fleet.main:app --host 0.0.0.0 --port 8000
```

### Ops CLI

```bash
# Health check — orphan worktrees, stale inbox, stuck agents, event storm, merge lock
uv run python -m fleet.cli doctor

# Backup DB + manifests to a tarball
uv run python -m fleet.cli backup --output ./backups
```

---

## 4. Architecture

### Module layout

```
fleet/
  config.py            # Settings (env vars + manifest loading), fail-closed validation
  db.py                # DatabaseManager: single-writer asyncio queue (ADR-001) + migrations
  models.py            # Pydantic domain models: AgentRecord, WorktreeRecord, Event, ...
  main.py              # FastAPI app — mounts all routers + lifespan wiring

  agents/              # Agent lifecycle: spawn, turn, inbox delivery, budgets,
  │                    #   compaction, heartbeat/hibernate
  │   backends/        #   protocol + Claude + MockBackend (JSONL transcript replay)
  │   promptbuild.py   #   layered prompt assembly (base + role + memory)
  │   budget.py        #   cost tracking + hard/soft budget enforcement

  workspace/           # git worktree create/remove, dirty-repo guard, ownership check
  policy/              # Role manifest loader + capability check (fail-closed)
  review/              # Conflict simulation, evidence gate, squash merge, merge lock
  approvals/           # Approval queue: create, decide, block/unblock agents
  memory/              # Typed project memory: ADR, known_bug, failed_attempt, recipe, ...
  events/              # Append-only event log + SSE hub (Last-Event-ID reconnect)

  api/                 # FastAPI routers: agents, events, tools, approvals, merge, review
  toolserver/          # MCP stdio entry point — thin relay to fleet API (ADR-002)
  dashboard/           # Jinja2 views + htmx + seed script
  templates/           # base.html, roster, conversation, timeline, worktree, validation,
  │                    #   approvals (_macros.html for reuse)
  static/app.js        # Vanilla JS SSE manager (no build step)

  manifests/           # default.yaml: role definitions, tool permissions, rate limits
  prompts/             # base.md + roles/*.md + modules/*.md (layered prompt system)
  migrations/          # 0001_init.sql: all table schemas

tests/
  fixtures/            # scripted git repo, JSONL mock transcripts, seeded DB
  manifests/           # test role manifests (permissive, restrictive)
  test_*.py            # 191 tests; mirrors package layout

docs/
  implementation/      # requirements, architecture, design, ADRs, validation report
  ops/                 # deploy.md (systemd + nginx), restore.md (backup recovery)
```

### Key design decisions

**ADR-001 — SQLite WAL with single-writer queue.** All DB writes go through one asyncio task consuming a queue; reads happen anywhere. No lock storms, deterministic event ordering. Migration to Postgres = swap engine + drop the queue.

**ADR-002 — MCP tool server as a separate stdio process.** In-process tool servers deadlock when a tool call needs the agent runtime that is blocked on the tool call. The tool server is a separate process, stateless, authenticated by `FLEET_API_TOKEN`, policy-checked on the API side. Restartable without killing agents.

**ADR-003 — MockBackend as a first-class citizen.** Agent lifecycle, budgets, compaction, inbox, and merge gate are all testable offline via JSONL transcript replay. CI needs no API key. New providers are adapter wrappers, not rewrites.

**ADR-004 — Append-only event log as source of truth.** Agent table rows are derived/cached state. Every mutation emits an event first. Crash recovery = replay the log on boot. Timeline is complete by construction.

**ADR-005 — Fail-closed everywhere.** Missing manifest/permission/evidence → hard error + actionable event. No silent defaults for safety-relevant config.

**ADR-006 — No auto-commits of user work.** Spawn against a dirty repo returns interactive options (continue-dirty / stash / commit / cancel). Only an explicit user choice mutates the repo, and that mutation is a `git_action` event.

---

## 5. Feature walkthrough

### Agent lifecycle

```
POST /api/agents
  { "name": "orchestrator", "role": "orchestrator", "repo_path": "/path/to/repo" }

→ 201  { "id": "agt-abc123", "status": "idle", ... }

POST /api/agents/agt-abc123/message
  { "content": "Implement the login feature" }

→ agent turns, emits tool_call / tool_result / agent_message events over SSE
→ auto-compacts at 80% context → writes memory → resumes fresh session
→ when done: POST /api/merge/agt-abc123/request (if evidence present)
→ approval_request event → human approves → squash merge → merge_result event
```

### Git worktree isolation

Each `spawn_worker` tool call creates a git worktree on a deterministic branch (`fleet/<task-id>-<name>`). Two workers with overlapping `owned_paths` globs are rejected at spawn. Dirty-repo detection blocks accidental worktree creation on uncommitted user changes.

### Policy manifests

```yaml
# manifests/default.yaml (excerpt)
roles:
  orchestrator:
    can_spawn: true
    tools: [spawn_worker, send_message, read_file, request_review]
    max_workers: 5
    spawn_rate_per_minute: 5
  worker:
    can_spawn: false
    tools: [read_file, write_file, run_tests, run_command, request_merge]
```

Unknown tool or role → deny + `policy_denied` event. Manifests are loaded at startup; changing a manifest requires a server restart (explicit, by design).

### Budget enforcement

Each agent tracks `cost_usd`. Crossing `budget_soft_usd` emits a `budget_alert` event. Crossing `budget_hard_usd` pauses the agent and opens an approval request that must be decided before the agent resumes.

### Evidence-gated merge

```
POST /api/merge/{agent_id}/request
```

Fleet checks:
1. Worktree is clean (no uncommitted files)
2. Conflict simulation against the base branch passes
3. Validation evidence exists (test results + exit code + summary)
4. Any policy-required reviewer verdict is present
5. No pending approval request blocks the agent

Missing any one → `422 Unprocessable Entity` with the specific gap identified.

### Web dashboard

Six views served at `/dashboard`:

| View | URL | What it shows |
|------|-----|---------------|
| Agent roster | `/dashboard/` | All agents: status, context %, cost, last event |
| Conversation | `/dashboard/agents/{id}` | SSE live tail of agent messages and tool calls |
| Event timeline | `/dashboard/events` | All events, filterable by type/agent/time; JSONL export |
| Worktree diff | `/dashboard/agents/{id}/worktree` | Branch diff summary |
| Validation | `/dashboard/agents/{id}/validation` | Evidence record, merge readiness gate |
| Approval queue | `/dashboard/approvals` | Pending requests with inline approve/deny (htmx) |

---

## 6. REST API overview

All endpoints require `Authorization: Bearer <FLEET_API_TOKEN>`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/agents` | Create agent |
| `GET` | `/api/agents` | List agents |
| `GET` | `/api/agents/{id}` | Get agent |
| `DELETE` | `/api/agents/{id}` | Terminate agent |
| `POST` | `/api/agents/{id}/message` | Send message |
| `GET` | `/api/agents/{id}/events` | SSE stream |
| `GET` | `/api/events` | Query event log |
| `POST` | `/api/merge/{id}/request` | Request merge (evidence-gated) |
| `GET` | `/api/approvals` | List pending approvals |
| `POST` | `/api/approvals/{id}/decide` | Approve or deny |
| `POST` | `/api/tools/{tool}` | MCP tool relay (used by tool server) |
| `GET` | `/api/review/{id}` | Reviewer verdict |

Errors follow RFC 7807: `{ "type": ..., "title": ..., "detail": ..., "status": ... }`.

---

## 7. Testing

```bash
# Full suite (offline, no API key)
uv run pytest -q -m "not live and not slow"
# 191 passed, 13 deselected (slow/live)

# Dashboard smoke (Playwright — requires a browser install)
uv run pytest tests/test_dashboard_smoke.py -m slow

# Live backend (requires ANTHROPIC_API_KEY)
ANTHROPIC_API_KEY=sk-... uv run pytest -m live
```

Test coverage by domain:

| Test file | What it covers |
|-----------|----------------|
| `test_agent_lifecycle.py` | Spawn, turn, interrupt, hibernate, restart recovery |
| `test_orchestration.py` | Orchestrator→worker golden flow (MockBackend) |
| `test_workspace.py` | Worktree create/remove, dirty-repo guard, ownership |
| `test_policy.py` | Capability deny, rate limit, fail-closed edge cases |
| `test_merge_gate.py` | Evidence gate, conflict detection, squash merge |
| `test_approvals.py` | Approval queue create/decide, block/unblock |
| `test_compaction.py` | Compaction trigger, memory writes, session resume |
| `test_budget.py` | Soft/hard budget enforcement, cost tracking |
| `test_reviewer_flow.py` | Reviewer role verdict + merge gate integration |
| `test_events.py` | Event append, SSE replay, Last-Event-ID reconnect |
| `test_cli.py` | `fleet doctor` checks, `fleet backup` tarball |
| `test_dashboard_smoke.py` | Playwright: all 6 views render with seeded DB |

---

## 8. Ops CLI

```bash
# Health report — exit 0 if all OK, exit 1 if any WARN
uv run python -m fleet.cli doctor [--db PATH]

Fleet doctor
============
[OK]   Orphan worktrees: 0
[WARN] Stale inbox: 3 messages, oldest 47m
[OK]   Event queue: normal
[WARN] Stuck agents: 1 agent (agt-xyz running for 15m)
[OK]   Merge lock: clear (process-local, always clear in CLI context)

# Backup — creates fleet-backup-<ts>.tar.gz with DB + manifests + metadata
uv run python -m fleet.cli backup [--db PATH] [--output DIR]
```

Doctor checks: orphan worktrees (active DB rows with missing disk path), stale inbox (pending messages older than 1 hour), event storm (>1000 events/minute), stuck agents (running > 600 s), merge lock held.

---

## 9. Production deployment

See [`docs/ops/deploy.md`](docs/ops/deploy.md) for the full systemd + nginx guide.

```bash
# Quick run
FLEET_API_TOKEN=<long-random-token> \
FLEET_DB_PATH=/var/lib/fleet/fleet.db \
uv run uvicorn fleet.main:app --host 127.0.0.1 --port 8000
```

Required env vars: `FLEET_API_TOKEN`, `FLEET_DB_PATH`, `FLEET_HOST`, `FLEET_PORT`.

Security: bind to `127.0.0.1`, put nginx in front, set `FLEET_API_TOKEN` to at least 32 random bytes. The tool server process inherits the token from the environment — rotate both together.

Backup and restore: [`docs/ops/restore.md`](docs/ops/restore.md).

---

## 10. What's next (post-MVP)

- **Task 7.3 — Telegram bridge:** mirror agent conversations to Telegram topics; route user replies back; transcribe voice notes.
- **Live test suite calibration:** record a Claude baseline against the golden orchestration flow; pin it as a regression gate.
- **Postgres migration:** swap SQLite engine for Postgres, drop the single-writer queue (ADR-001). Schema is Postgres-ready.
- **Historical trend dashboard:** per-agent cost and context charts over time.

---

## Document map

| Doc | Purpose |
|-----|---------|
| `docs/implementation/requirements.md` | Numbered functional/non-functional requirements + acceptance criteria |
| `docs/implementation/architecture.md` | Module boundaries, ADRs, constraints |
| `docs/implementation/design.md` | Flows, views, data model, failure modes, security |
| `docs/implementation/design-system.md` | Dashboard design tokens |
| `docs/implementation/implementation-plan.md` | Phased task checklist |
| `docs/implementation/validation-report.md` | Final gate: 191 tests, all ACs, risk register |
| `docs/ops/deploy.md` | Production deployment (systemd + nginx) |
| `docs/ops/restore.md` | Backup recovery procedure |

---

## Honesty checklist

- All tests run offline against `MockBackend` (JSONL transcript replay) — no API key, no token cost in CI.
- The live backend (`claude.py`) is implemented; `test_claude_adapter.py` is marked `live` and skipped unless `ANTHROPIC_API_KEY` is set.
- Thresholds (rate limits, budget defaults, compaction trigger) are in `manifests/default.yaml` and `config.py` — not hardcoded in business logic.
- The merge gate is fail-closed by design: missing evidence → 422, never a silent pass.
- No auto-commits of user work, ever (ADR-006).
