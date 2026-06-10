# Design

Date: 2026-06-10
Status: Reviewed

## Product Flow

- Entry: user registers a repository (path, default branch, merge policy), then creates an
  orchestrator for it from the dashboard or CLI.
- Main workflow: user messages the orchestrator ("build X") → orchestrator decomposes and
  `spawn_worker`s into worktrees → workers execute, message back progress/results →
  orchestrator requests review where policy demands → validation evidence recorded → merge
  gate → squash to default branch → orchestrator reports to user with cost summary.
- Steering: user can message any agent mid-task; messages inject mid-turn.
- Empty/loading/error/success states: specified per view below; all five states mandatory.
- Mobile/desktop: dashboard is desktop-first; read + approve flows must work at 390px width
  (approvals from a phone is a real use case).

## Screens / Views (dashboard)

### Agent Roster
- Purpose: one-glance fleet state.
- Data shown: per agent — name, role badge, model, status (idle/running/waiting/paused_budget/
  failed/archived), context_pct bar, cost_usd today/total, parent, worktree branch, last
  activity relative time.
- Actions: open conversation, send message, interrupt, compact, archive; "New orchestrator"
  CTA.
- Error states: backend unreachable banner with retry; per-agent failed status shows last
  error event inline.
- Tests: Playwright — seeded 6-agent roster renders all statuses; empty state shows CTA.

### Agent Conversation
- Purpose: full transcript of one agent.
- Data shown: interleaved user/agent messages, tool calls (collapsed, expandable args/result),
  inter-agent messages tagged with sender, turn separators with cost/tokens/duration, live
  tail via SSE.
- Actions: send message, interrupt, jump-to-event-in-timeline.
- Error states: SSE drop → reconnect indicator + Last-Event-ID catch-up; send failure toast
  with retry.
- Tests: SSE catch-up integration test; long-transcript pagination.

### Event Timeline
- Purpose: audit view across the project.
- Data shown: filterable stream (by agent, event type, time range); each row: ts, agent,
  type icon, one-line summary; expand for payload.
- Actions: filter, export range as JSONL.
- Tests: filter combinations against seeded events; export round-trips.

### Worktree / Diff
- Purpose: review what a worker actually changed.
- Data shown: branch, base, ahead/behind counts, dirty files list, per-file diff stat, diff
  viewer (server-rendered, syntax highlighted).
- Actions: request conflict check, open merge flow.
- Tests: WIP endpoint golden test against a scripted repo.

### Validation & Merge
- Purpose: evidence-gated merge.
- Data shown: task acceptance criteria, validation evidence table (command, exit code,
  summary, ts), reviewer verdict (if any), conflict-simulation result, merge policy checklist
  with pass/fail per item.
- Actions: run checks, request review, approve & merge (disabled until checklist green).
- Error states: failing item shows exact failing command output excerpt.
- Tests: merge blocked without evidence (AC-013) exercised through the UI.

### Approval Queue
- Purpose: human gate for destructive/costly ops.
- Data shown: pending requests — requester agent, operation, rationale, risk note, age;
  history of decisions.
- Actions: approve / deny with optional comment (both become events).
- Tests: approve/deny round-trip updates agent state (AC-033).

## System Architecture

- Frontend: Jinja templates + htmx + one `app.js` (SSE wiring, diff expand); no build step.
- Backend: FastAPI app composed of routers (`agents`, `workspaces`, `events`, `tools`,
  `approvals`, `merge`, `dashboard`); domain services behind routers; single process,
  asyncio.
- Storage: SQLite WAL via SQLAlchemy Core; append-only `events`; Postgres-compatible schema.
- External services: model providers via backend adapters; git via subprocess with timeout;
  optional Telegram (post-MVP) in its own module speaking only to the public API.
- AI/model layer: `AgentBackend` protocol (start/send/events/interrupt/stop/context_usage);
  adapters: Claude (first), Mock (deterministic, test fixture-driven), OpenAI/Codex (later).
- Tool/function calls: MCP stdio subprocess exposing the tool surface; calls relayed to the
  API over localhost HTTP with internal token; policy checked server-side (never trust the
  tool process).

## Model behavior (AI design)

- Prompt layers, in order, each with a token budget: platform rules (≤800 tok) → workspace
  rules (≤400) → role prompt (≤1200) → task prompt (user-supplied) → team state snapshot
  (≤600, generated) → retrieved memory (≤800) → tool list (generated from policy). Missing
  role prompt = hard error at spawn (fail-closed; no silent base fallback).
- Orchestrator behavior contract: decompose → spawn → monitor → verify evidence → report.
  It does not edit files; tool policy enforces this, not just the prompt.
- Worker behavior contract: before-work checklist (read task, check WIP, confirm owned
  paths); before-done checklist (tests run, evidence recorded via `record_validation`,
  message orchestrator).
- Fallbacks: backend stream error → one transparent resume attempt from last session id →
  failed state + error event + orchestrator notification. Tool timeout → tool_result error
  the agent sees verbatim.
- Eval cases (run in CI with MockBackend; live smoke optional): delegation correctness
  (orchestrator spawns ≤N workers with non-overlapping paths for a canned task), evidence
  discipline (worker that skips `record_validation` cannot merge), injection resistance
  (malicious tool_result content cannot trigger unauthorized tool call — policy denies),
  budget compliance (agent acknowledges pause).
- Abuse/failure modes: prompt-injected repo content instructing exfiltration → secret-path
  read denial + network-tool denial are policy-level, tested; agent spam-looping spawn →
  rate limit (REQ-028).

## Data Model

```text
repositories(id TEXT PK, path TEXT UNIQUE, default_branch TEXT, merge_policy_json TEXT,
             created_at TEXT)
agents(id TEXT PK, name TEXT, scope TEXT, role TEXT, backend TEXT, model TEXT,
       status TEXT CHECK(status IN ('idle','running','waiting','paused_budget','failed',
       'archived')), parent_id TEXT NULL REFERENCES agents(id), repository_id TEXT NULL,
       session_ref TEXT NULL, worktree_id TEXT NULL, context_pct REAL DEFAULT 0,
       cost_usd REAL DEFAULT 0, budget_soft_usd REAL NULL, budget_hard_usd REAL NULL,
       created_at TEXT, updated_at TEXT, UNIQUE(scope, name))
worktrees(id TEXT PK, agent_id TEXT REFERENCES agents(id), repository_id TEXT,
          path TEXT, branch TEXT, base_branch TEXT, owned_paths_json TEXT,
          status TEXT CHECK(status IN ('active','merged','removed')), created_at TEXT)
events(id INTEGER PK AUTOINCREMENT, ts TEXT, scope TEXT, agent_id TEXT NULL,
       type TEXT, summary TEXT, payload_json TEXT)            -- append-only, indexed (scope, id)
inbox(id INTEGER PK AUTOINCREMENT, to_agent_id TEXT, sender TEXT, message TEXT,
      status TEXT CHECK(status IN ('pending','delivered','failed')), created_at TEXT,
      delivered_at TEXT NULL)                                  -- FIFO per to_agent_id
tasks(id TEXT PK, scope TEXT, title TEXT, description TEXT, status TEXT,
      owner_agent_id TEXT NULL, branch TEXT NULL, acceptance_criteria_json TEXT,
      created_at TEXT, updated_at TEXT)
validation_evidence(id INTEGER PK, task_id TEXT REFERENCES tasks(id), command TEXT,
      exit_code INTEGER, summary TEXT, skipped TEXT NULL, residual_risk TEXT NULL, ts TEXT)
approvals(id TEXT PK, scope TEXT, requester_agent_id TEXT, operation TEXT,
      rationale TEXT, risk TEXT, status TEXT CHECK(status IN ('pending','approved','denied')),
      decided_by TEXT NULL, comment TEXT NULL, created_at TEXT, decided_at TEXT NULL)
memory(id TEXT PK, scope TEXT, kind TEXT CHECK(kind IN ('architecture_decision','known_bug',
      'failed_attempt','command_recipe','dependency_note','deployment_note')),
      title TEXT, body TEXT, source_event_id INTEGER NULL, created_at TEXT)
usage_snapshots(id INTEGER PK, scope TEXT, ts TEXT, cost_usd REAL, tokens INTEGER)
```

## Failure Modes

- Backend stream dies mid-turn: one auto-resume from session_ref; else agent → failed,
  error event, orchestrator notified by system message. User sees red status + last error.
- SQLite locked: busy_timeout 5 s; writes funneled through a single writer task; if still
  failing, event buffered in memory (bounded, 1k) and flushed — overflow is a fatal,
  logged condition (never silent drop).
- Git command failure (worktree add/merge): no partial state — operations are
  check-then-act with cleanup on exception; failure surfaces as git_action event with stderr.
- MCP process crash: supervisor restarts it; in-flight tool call returns timeout error to the
  agent; restart is a state_change event.
- Turn timeout: interrupt issued, error event, agent back to idle; orchestrator notified.
- Approval never answered: request ages visibly in queue; agent stays blocked (waiting
  status), heartbeat keeps session alive; no auto-approve, ever.

## Observability

- Logs: structured JSON to stdout (uvicorn + app logger), one line per event write.
- Metrics/events: cost_usd, tokens, context_pct, turn duration per turn_end event;
  per-scope usage_snapshots every 15 min; counts by event type queryable.
- Error tracking: error events carry exception class + truncated trace; dashboard surfaces
  per-agent last error.

## Security and Privacy

- Secrets: env-only; startup check refuses to boot if `FLEET_API_TOKEN` unset on non-local
  bind. Event payload writer scrubs strings matching configured secret env values.
- PII/data retention: transcripts stay local in SQLite; export is explicit; no telemetry.
- Auth/authorization: every API route except dashboard static assets requires the internal
  token; the dashboard obtains a session via the same token entered once.
- Abuse prevention: tool policy fail-closed; secret-path deny list; shell tool with cwd
  pinning, env allowlist, timeout; spawn rate limits; budgets with hard pause.

## Design Review

PM: flows cover the brief; approvals-from-phone noted as the only mobile-critical path. OK.
Developer: single-writer SQLite + asyncio is implementable; adapter protocol matches SDK
  realities; flagged compaction as the riskiest piece → isolated behind Agent Service with
  MockBackend tests first. OK.
Tester: every AC maps to a concrete test hook (MockBackend, scripted git repo fixture,
  seeded DB, Playwright). Asked for the golden event-sequence test — added (AC-020). OK.
Reviewer: checked for silent-fallback patterns from prior art — role-prompt fail-closed and
  no-auto-commit rules are explicit requirements, not conventions. Flagged event payload
  scrubbing — added to Security. OK.
Team Lead: phase order matches dependency graph; merge gate (Phase 6) correctly depends on
  evidence model (Phase 5). Approved.
