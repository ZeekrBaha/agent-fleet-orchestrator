# Research

Date: 2026-06-10
Status: Reviewed

## Goal

A self-hosted platform where a user runs a persistent team of AI coding agents against real
git repositories: an orchestrator agent decomposes work and delegates; worker agents execute
in isolated git worktrees; agents communicate by explicit messages; every action is an
auditable event; merging to main requires validation evidence. The user supervises through a
live dashboard (and later a Telegram bridge) instead of babysitting terminal sessions.

## Users

- **Solo developer / small team lead** (primary): hands a feature or refactor to the
  orchestrator, monitors the dashboard, approves merges and destructive actions.
- **Agent operator** (secondary): tunes role manifests, tool policies, budgets, and prompts
  per project.

## Evidence

- Repository fact: this is a greenfield project; no existing code constraints.
- Internal research note: a deep architecture analysis of a mature open-source
  persistent-agent orchestration platform was completed (2026-06-09). Validated patterns:
  persistent per-agent async event loops; git-worktree-per-worker isolation; message-based
  inter-agent communication via an inbox table with mid-turn injection; MCP tool server in a
  separate process to avoid SDK re-entrancy deadlocks; WAL-mode SQLite; prompt layering
  (platform rules + role + skills); YAML pipeline manifests. Validated weaknesses to avoid:
  1,500-line route monoliths; untyped session dicts; silent fallback when a role prompt is
  missing; no rate limiting on spawn; auto-WIP-commit surprises; test gaps on the HTTP layer.
- Repository fact (prior draft): `~/Desktop/orchestrator-claude.md` contains an approved
  high-level plan (service boundaries, 7 phases, security rules). This package supersedes and
  extends it.
- External source: Claude Agent SDK (`claude-agent-sdk` ≥ 0.1.x) supports session resume and
  per-agent MCP server injection (checked 2026-06-10 via SDK docs).
- Assumption: a single host with one OS user runs the platform and all agents (single-tenant
  MVP). Multi-tenant isolation is explicitly deferred.
- Assumption: users have `git` ≥ 2.40 and at least one agent CLI/SDK credential configured.

## Constraints

- Stack: Python 3.12+, FastAPI, SQLite (WAL) via SQLAlchemy Core, Pydantic v2, uv, pytest +
  pytest-asyncio, ruff, mypy (strict where practical), MCP stdio server, git CLI, SSE.
  Dashboard: server-rendered Jinja + htmx + vanilla JS for MVP (single runtime, no node build
  step); typed React client deferred to product phase. **Note:** this overrides the skill's
  Next.js default — the product is a Python control plane whose UI is an ops dashboard; one
  runtime beats two for the MVP, and SSE consumption from Jinja/htmx is proven.
- Existing architecture: none (clean-room). Reuse ideas from prior art, never code; the
  reference platform is AGPL — zero code copying, zero file-structure mirroring.
- Required APIs/models: Anthropic (Claude) adapter first; adapter protocol must also admit
  OpenAI/Codex and a deterministic MockBackend. No hardcoded model IDs outside role manifests.
- Environment/secrets: `FLEET_DB_PATH`, `FLEET_API_TOKEN` (internal auth), provider keys
  (`ANTHROPIC_API_KEY`, optional `OPENAI_API_KEY`), optional `TELEGRAM_BOT_TOKEN`. Names only —
  values never in code, prompts, logs, or events.
- Budget/performance: target ≤ 25 concurrent agents on one machine; event writes ≤ 5 ms p95;
  SSE fan-out ≤ 200 ms from event to dashboard; per-agent and per-task USD budgets enforced.
- Accessibility/privacy/security: dashboard WCAG AA; agents must never read `.env`, `~/.ssh`,
  cloud credential stores; all tool input validated; destructive actions behind approval.

## Unknowns

- [ ] Which agent SDK version pins are stable for long-lived sessions (resume across server
      restart)? Verify in Phase 2 spike before committing the adapter API.
- [ ] SQLite write contention at 25 agents × high-frequency events — is a single writer
      thread enough, or do we need batched event inserts? Measure in Phase 1 with a synthetic
      load test.
- [ ] MCP stdio auth: token via env vs per-spawn nonce. Decide in Phase 4 ADR.

## Risks

- Agents over-empowered → unsafe changes: capability-based tool policy, fail-closed, approval
  queue for destructive ops (owner: Tool Policy Service).
- Git automation corrupts user work: never auto-commit user's dirty tree; dirty-repo check
  blocks spawn with explicit options (owner: Workspace Service).
- Event log becomes noise: typed event taxonomy + per-view filters + compaction summaries
  (owner: Event Log Service).
- Prompt bloat degrades agent quality: layered prompts with budget per layer; measured via
  context_pct telemetry (owner: prompt builder).
- Cost runaway: hard per-agent/task USD ceilings; agent paused + approval event on breach
  (owner: Agent Service).
- Scope creep before core works: phases gated; Telegram/jobs/memory deferred until Phase 7+.

## Options Considered

1. **Persistent agent fleet (chosen):** agents live days, resume from disk, communicate by
   messages. Pros: matches how real teams work; cheap idle agents; mid-task steering.
   Cons: lifecycle complexity (heartbeat, hibernation, compaction). Choose when tasks span
   hours and need human steering.
2. **One-shot DAG pipeline (LangGraph-style):** pros: simple replay semantics; cons: no
   mid-task steering, no persistent context, poor fit for long coding tasks. Rejected.
3. **Single shared checkout with file locks instead of worktrees:** pros: less disk; cons:
   lock complexity, no branch-level rollback, agents clobber each other. Rejected — worktree
   isolation is filesystem-enforced and trivially rolled back by deleting a branch.
4. **Postgres from day one:** pros: no migration later; cons: deployment overhead for a
   single-host tool. Rejected for MVP — SQLite WAL with a Postgres-ready schema (no SQLite-only
   types, UUID text PKs, ISO timestamps).
