# Fleet Implementation Plan

Date: 2026-06-10
Status: Draft (awaiting approval gate)

## Goal
Working MVP: one orchestrator + workers in worktrees, message-based coordination, evidence-
gated squash merge, live SSE dashboard, all state restart-safe — per requirements.md.

## Source Documents
- `docs/implementation/research.md`
- `docs/implementation/requirements.md`
- `docs/implementation/design.md`
- `docs/implementation/design-system.md`
- `docs/implementation/architecture.md`

## Global rules for every task
- TDD: failing test first, watch it fail for the right reason, minimal code, watch it pass.
- `uv run ruff check .`, `uv run mypy fleet`, `uv run pytest -q` green before every commit.
- Every new tool/endpoint: Pydantic schema + permission + audit events + tests, or it does
  not merge (REQ-027).
- Conventional commits; one concern per commit.

## Task Checklist

### Phase 1 — Foundation

#### Task 1.1: Scaffold + config
Owner role: Developer
Files likely touched: `pyproject.toml`, `fleet/config.py`, `fleet/models.py`,
`tests/test_config.py`
Steps:
- [ ] uv project, deps (fastapi, uvicorn, sqlalchemy, pydantic, pyyaml, httpx, mcp, jinja2),
      dev deps (pytest, pytest-asyncio, ruff, mypy, playwright)
- [ ] Settings from env (fail on missing FLEET_API_TOKEN for non-local bind)
Acceptance criteria:
- [ ] Boot refuses non-local bind without token (test)
Tests: unit: config validation matrix.
Validation commands: `uv run pytest tests/test_config.py -q`
Risks/rollback: none — additive.

#### Task 1.2: DB layer + migrations + single writer (ADR-001)
Owner role: Developer
Files: `fleet/db.py`, `fleet/migrations/0001_init.sql`, `tests/test_db.py`
Steps:
- [ ] Schema from design.md Data Model; WAL, busy_timeout, foreign_keys pragmas
- [ ] Writer task with queue; write() awaits durable commit
- [ ] Synthetic load test: 25 producers × 200 events, zero loss, p95 ≤ 5 ms (resolves
      research unknown #2)
Acceptance: maps to NFR performance/reliability.
Tests: migration idempotency; writer ordering; load test (marked `slow`).
Commands: `uv run pytest tests/test_db.py -q`
Risks: SQLite contention → batching fallback documented in ADR-001.

#### Task 1.3: Event Log Service + SSE hub
Owner role: Developer
Files: `fleet/events/{service,sse}.py`, `fleet/api/events.py`, `tests/test_events.py`
Steps:
- [ ] append(event) → id; query(scope, filters, after_id)
- [ ] SSE endpoint with Last-Event-ID catch-up (AC-022)
Acceptance: AC-020 groundwork, AC-022.
Tests: unit append/query; integration SSE catch-up with simulated disconnect.
Commands: `uv run pytest tests/test_events.py -q`

### Phase 2 — Agent Runtime

#### Task 2.1: Backend protocol + MockBackend (ADR-003)
Owner role: Developer
Files: `fleet/agents/backends/{protocol,mock}.py`, `tests/fixtures/transcripts/*.jsonl`,
`tests/test_mock_backend.py`
Steps:
- [ ] Protocol per design; MockBackend replays JSONL transcript (text/tool_use/turn_end
      events with cost/context metadata)
Tests: transcript replay determinism; interrupt mid-transcript.

#### Task 2.2: Agent Service lifecycle + inbox
Owner role: Developer
Files: `fleet/agents/{service,session,inbox}.py`, `fleet/api/agents.py`,
`tests/test_agent_lifecycle.py`, `tests/test_inbox.py`
Steps:
- [ ] create/list/send/interrupt/archive; status machine per design
- [ ] inbox FIFO at-least-once; mid-turn injection at safe point; restart-safe pending rows
- [ ] heartbeat, hibernate after idle, turn timeout 300 s
- [ ] restart restore (AC-003) — kill/restore integration test with MockBackend
Acceptance: AC-001 (mock path), AC-003, AC-004.
Commands: `uv run pytest tests/test_agent_lifecycle.py tests/test_inbox.py -q`
Risks: async leak on interrupt → use task groups; assert no pending tasks in tests.

#### Task 2.3: Claude adapter (spike then implement; resolves research unknown #1)
Owner role: Developer
Files: `fleet/agents/backends/claude.py`, `tests/test_claude_adapter.py` (offline: parser
units; live marked `live`)
Steps:
- [ ] Session start/resume via SDK; event normalization to protocol; context_usage from
      SDK metadata
Tests: event parsing from recorded SDK fixtures (offline); one `live`-marked smoke.

#### Task 2.4: Budgets
Owner role: Developer
Files: `fleet/agents/budget.py`, `tests/test_budget.py`
Steps:
- [ ] accumulate cost per turn_end; soft → budget_alert event; hard → paused_budget +
      approval request (AC-032)

### Phase 3 — Worktree Isolation

#### Task 3.1: Repo registry + scripted-repo test fixture
Owner role: Tester (fixture) + Developer
Files: `fleet/workspace/{service,gitops}.py`, `tests/fixtures/gitrepo.py`,
`tests/test_workspace.py`
Steps:
- [ ] register repo (path validation, default branch detect)
- [ ] fixture builds throwaway repos with controllable dirty/conflict states

#### Task 3.2: Worktree create/remove + dirty gate + ownership (ADR-006)
Owner role: Developer
Files: same as 3.1
Steps:
- [ ] branch naming `fleet/<task-id>-<name>`; create/remove with cleanup-on-failure
- [ ] dirty-repo 409 with four options (AC-011); explicit option execution as git_action
      events
- [ ] owned_paths glob overlap rejection (AC-014)
- [ ] WIP report endpoint (AC for REQ-012)
Commands: `uv run pytest tests/test_workspace.py -q`
Risks: worktree leftovers on crash → `fleet doctor` cleanup command listed in Phase 7.

### Phase 4 — MCP Tool Server

#### Task 4.1: toolserver process + auth ADR
Owner role: Developer
Files: `fleet/toolserver/main.py`, `fleet/api/tools.py`, `tests/test_toolserver.py`
Steps:
- [ ] stdio MCP server; relays to API with FLEET_API_TOKEN; write the auth ADR (research
      unknown #3)
- [ ] tools v1: spawn_worker, send_message, list_agents, get_agent_logs, stop_agent,
      worker_wip, check_conflict, record_validation, report_issue, update_progress,
      request_approval, memory_write
Acceptance: AC-026 (policy denial), AC-028 (rate limit), tool audit events.

#### Task 4.2: Tool Policy Service (fail-closed, ADR-005)
Owner role: Developer
Files: `fleet/policy/{service,rules}.py`, `fleet/manifests/default.yaml`,
`tests/test_policy.py`
Steps:
- [ ] role manifest loader (validation: fail-closed; unknown role/tool → deny)
- [ ] secret-path deny list; shell rules (timeout, cwd pin, env allowlist); spawn rate
      limits per scope
- [ ] fuzz invalid tool inputs → typed validation errors (AC-026)

### Phase 5 — Orchestrator/Worker Flow

#### Task 5.1: Prompt builder (layered, budgeted)
Owner role: Developer
Files: `fleet/agents/promptbuild.py`, `fleet/prompts/*`, `tests/test_promptbuild.py`
Steps:
- [ ] layers + token budgets per design; missing role prompt → hard error (ADR-005)
- [ ] memory retrieval injection within budget (AC-040 part)

#### Task 5.2: spawn_worker end-to-end + tasks + evidence model
Owner role: Developer
Files: `fleet/agents/service.py`, `fleet/review/evidence.py`, `tests/test_orchestration.py`
Steps:
- [ ] orchestrator (mock) spawns worker (mock) in worktree; messages round-trip;
      parent/child links
- [ ] tasks + validation_evidence tables wired; `record_validation` tool writes evidence
- [ ] golden event-sequence test for canned flow (AC-020)
Acceptance: AC-001 complete, AC-020.

#### Task 5.3: Compaction + memory
Owner role: Developer
Files: `fleet/agents/compaction.py`, `fleet/memory/service.py`, `tests/test_compaction.py`
Steps:
- [ ] threshold trigger; summarize via backend; typed memory write; fresh session resume
      (AC-007, AC-040)

### Phase 6 — Merge and Review

#### Task 6.1: Conflict simulation + merge gate + squash (evidence-gated)
Owner role: Developer
Files: `fleet/review/{merge,conflict,lock}.py`, `fleet/api/merge.py`,
`tests/test_merge_gate.py`
Steps:
- [ ] conflict check via temporary merge in detached worktree (never mutates default branch)
- [ ] gate checklist: clean tree, evidence present, approvals, reviewer verdict when policy
      requires; squash with task-linked message (AC-013)
- [ ] test lock per scope (serialize merges)

#### Task 6.2: Approval queue
Owner role: Developer
Files: `fleet/approvals/service.py`, `fleet/api/approvals.py`, `tests/test_approvals.py`
Steps:
- [ ] create/decide; agent block/unblock wiring (AC-033); budget-pause resume (AC-032)

#### Task 6.3: Reviewer role
Owner role: Developer
Files: `fleet/manifests/default.yaml`, `fleet/prompts/roles/reviewer.md`,
`tests/test_reviewer_flow.py`
Steps:
- [ ] reviewer role manifest (cross-family model slot); verdict event consumed by gate
      (AC for REQ-031)

### Phase 7 — Dashboard + Hardening

#### Task 7.1: Dashboard views (tokens from design-system.md, all states)
Owner role: Developer + Junior Developer
Files: `fleet/dashboard/*`, `tests/test_dashboard_smoke.py` (Playwright)
Steps:
- [ ] Roster, Conversation (SSE), Timeline, Worktree/Diff, Validation & Merge, Approval
      Queue — exactly as specified in design.md, tokens applied verbatim
- [ ] seeded-DB Playwright smoke incl. empty/loading/error states (AC-050)
- [ ] Anti-Slop Visual Gate from validation-plan.md

#### Task 7.2: Ops hardening
Owner role: Developer
Files: `fleet/cli.py` (`fleet doctor`, backup/restore), docs
Steps:
- [ ] doctor: orphan worktrees, stale inbox, unflushed events
- [ ] backup = copy db + manifests; restore doc; deployment doc (uvicorn + systemd)

#### Task 7.3 (post-MVP): Telegram bridge
Owner role: Developer — separate module, public-API-only, deferred until MVP ships.

## Requirement → Task map
REQ-001/002→5.2 · 003→2.2 · 004→2.2 · 005→2.2 · 006→2.2 · 007→5.3 · 010–014→3.2 ·
020→1.3/5.2 · 021→2.2 · 022→1.3 · 025→4.1 · 026/027→4.2 · 028→4.2 · 030→5.2 · 031→6.3 ·
032→2.4/6.2 · 033→6.2 · 040→5.3 · 050→7.1 · 051→all api tasks · 052→7.3

## Role Review Notes
PM: phases each end in a demoable artifact; MVP value lands at Phase 6, dashboard completes
  it at 7. Telegram correctly out of MVP.
Developer: riskiest items (SDK resume, SQLite contention, compaction) all have early spikes
  or load tests; adapter protocol isolates provider risk.
Junior Developer: tasks reference exact files and ACs; fixtures (gitrepo, transcripts) are
  defined before the tasks that need them.
Tester: every AC has an owning task; golden event-sequence test pins the core flow; fuzz
  cases on tool inputs explicit.
Reviewer: clean-room rule restated in architecture constraints; no task says "port X" —
  all specs are behavioral. Budget/approval paths have negative tests.
Team Lead: sequencing respects dependencies (events → agents → worktrees → tools → flow →
  merge → UI); CI gates defined in validation-plan.md from Phase 1.

## Approval Gate
- [ ] Research approved
- [ ] Design approved
- [ ] Plan approved
- [ ] Coding may begin (Phase 1 only)
