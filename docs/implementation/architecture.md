# Architecture

Date: 2026-06-10
Status: Approved

## Current Architecture
- Repository facts: greenfield; spec-only repo. No legacy constraints.
- Important inherited patterns (from research, ideas only — clean room): persistent per-agent
  async loops, worktree isolation, inbox messaging, out-of-process MCP, WAL SQLite, layered
  prompts, YAML role manifests.

## Proposed Architecture

- Modules (Python package `fleet/`):
```text
fleet/
  config.py            # env + manifest loading, fail-closed validation
  db.py                # engine, single-writer task, migrations
  models.py            # Pydantic domain models (AgentRecord, WorktreeRecord, Event, ...)
  events/              # Event Log Service: append, query, SSE hub, retention
  agents/              # Agent Service: lifecycle, turns, inbox delivery, budgets,
                       #   compaction, heartbeat/hibernate; backends/ (protocol, claude,
                       #   mock, openai)
  workspace/           # Workspace Service: repo registry, worktree create/remove, WIP,
                       #   dirty checks, ownership validation (git via subprocess)
  policy/              # Tool Policy Service: role manifests, capability checks,
                       #   secret-path/command/network rules, rate limits
  review/              # Review/Merge Service: conflict simulation, evidence gate,
                       #   squash merge, reviewer verdicts, test lock
  approvals/           # approval queue: create, decide, block/unblock agents
  memory/              # typed project memory: write, retrieve (budgeted)
  toolserver/          # MCP stdio process (separate entrypoint) — thin HTTP relay
  api/                 # FastAPI routers per domain + auth dependency
  dashboard/           # Jinja templates, htmx views, static/
  prompts/             # base.md, roles/*.md, modules/*.md (layered)
  manifests/           # default.yaml role/pipeline manifests
tests/                 # mirrors package layout + fixtures/ (scripted git repo, mock
                       # transcripts, seeded db)
```
- Boundaries: Agent Service never runs git; Workspace Service never sees prompts; policy
  checks happen in the API layer (server-side), never in the tool process; dashboard consumes
  only public API.
- Dependencies: api → services → db/models; toolserver → api (HTTP only); no service-to-
  service imports except through interfaces in `models.py`.
- Data flow (happy path): user msg → api → agents.inbox → backend turn → events appended →
  SSE hub → dashboard; tool call → toolserver → api → policy → service → tool_result event →
  back to backend.
- API contracts: all request/response bodies are Pydantic models; errors are RFC7807-style
  `{type, title, detail, status}`.

## Decisions

### ADR-001: SQLite WAL with single-writer task, Postgres-ready schema
- Context: single-host MVP, many async writers, future product DB.
- Options: (a) direct writes from any task; (b) single writer queue; (c) Postgres now.
- Decision: (b) — all writes go through one asyncio task consuming a queue; reads anywhere.
- Consequences: no lock storms; ordering guarantee for events; one place to batch; migration
  to Postgres = swap engine + drop queue.

### ADR-002: MCP tool server as separate stdio process relaying over HTTP
- Context: in-process tool servers deadlock when a tool call needs the agent runtime that is
  blocked on the tool call.
- Decision: separate process per platform (not per agent), authenticated with
  `FLEET_API_TOKEN`, stateless; policy enforced API-side.
- Consequences: tool process is untrusted by design; restartable without killing agents.

### ADR-003: Backend adapter protocol with MockBackend as a first-class citizen
- Context: provider neutrality + testability of lifecycle logic without API keys.
- Decision: `AgentBackend` protocol (`start/send/events/interrupt/stop/context_usage`);
  MockBackend driven by JSONL transcript fixtures, used by most tests and CI.
- Consequences: lifecycle, budgets, compaction, inbox, merge-gate all testable offline; new
  providers are adapters, not rewrites.

### ADR-004: Append-only event log as source of truth; agent rows are projections
- Context: audit, replay, debugging; avoiding untyped session dicts.
- Decision: every mutation emits an event first (write-ahead at domain level); `agents`
  table fields are derived/cached state updated in the same transaction.
- Consequences: timeline is complete by construction; replays/exports trivial; slight write
  amplification accepted.

### ADR-005: Fail-closed everywhere user intent is ambiguous
- Context: prior art shows silent fallbacks (missing role prompt → base prompt) breed
  invisible bugs.
- Decision: missing prompt/manifest/permission/evidence → hard error event + actionable
  message. No silent defaults for safety-relevant config.
- Consequences: more friction on first run; predictable behavior under drift.

### ADR-006: No auto-commits of user work; dirty-repo spawn is interactive
- Context: surprising auto-WIP commits are a top complaint in prior art.
- Decision: spawn against dirty repo returns options (continue-dirty/stash/commit/cancel);
  only explicit user choice mutates the repo, and that mutation is a git_action event.
- Consequences: an extra round-trip sometimes; user trust always.

## Implementation Constraints
- Do not touch: nothing exists yet — but never vendor or paraphrase AGPL code from prior art.
- Must reuse: `models.py` Pydantic types across services (no ad-hoc dicts crossing
  boundaries); the single writer for every DB mutation; the event taxonomy from design.md.
- Must avoid: module > ~500 lines (split first); business logic in routers; `except
  Exception` without a narrow re-raise or typed mapping; model IDs hardcoded outside
  manifests; tool registration without schema + permission + tests.
