# Requirements

Date: 2026-06-10
Status: Approved

## Functional Requirements

### Agent lifecycle
- REQ-001: User can create an orchestrator agent bound to a registered repository.
- REQ-002: An orchestrator can spawn worker agents with a name, role, task description, and
  model; spawn is rejected if the name is taken in scope or role manifest forbids the spawn.
- REQ-003: Agents persist across server restarts: status, conversation session id, worktree
  binding, and cost counters are restored from the database on boot.
- REQ-004: User or orchestrator can send a message to any agent; an idle agent starts a turn,
  a busy agent receives it as a mid-turn injection at the next safe point.
- REQ-005: User can interrupt an agent's current turn; the agent returns to idle without
  losing session history.
- REQ-006: Agents hibernate after a configurable idle period and resume on next message; a
  turn exceeding the turn timeout (default 300 s) is interrupted and logged as an error event.
- REQ-007: An agent whose context usage crosses a threshold (default 80%) is compacted: the
  session is summarized, durable facts are written to project memory, and a fresh session
  resumes with the summary.

### Workspace and git safety
- REQ-010: Each coding worker gets its own git worktree on its own branch; branch naming is
  deterministic (`fleet/<task-id>-<name>`).
- REQ-011: Spawning a worktree from a dirty repository is blocked; the user is offered:
  continue-dirty (explicit), stash, commit, or cancel. No automatic commits of user work, ever.
- REQ-012: Worker WIP (uncommitted files + commits ahead of base) is inspectable via API/tool
  without touching the worktree.
- REQ-013: Merge to the default branch is allowed only when: worktree is clean, conflict
  simulation passes, validation evidence exists (REQ-030), and policy-required approvals are
  granted. Merge is a squash producing one commit linked to the task.
- REQ-014: Ownership paths: two live workers may not claim overlapping path globs; overlap is
  rejected at spawn.

### Communication and events
- REQ-020: Every meaningful action produces exactly one typed, append-only event
  (user_message, agent_message, tool_call, tool_result, state_change, file_change, git_action,
  test_result, review_verdict, merge_result, approval_request, approval_decision, budget_alert,
  error).
- REQ-021: Inter-agent messages go through an inbox with at-least-once delivery and per-agent
  FIFO ordering; undelivered messages survive restart and are retried.
- REQ-022: Dashboard receives events over SSE; a client reconnecting with Last-Event-ID
  receives missed events (no gaps for the retained window).

### Tools and policy
- REQ-025: Agents access platform capabilities only via MCP tools served by a separate stdio
  process authenticated to the API with an internal token.
- REQ-026: Tool access is capability-based per role manifest; a call to an unassigned tool is
  rejected and logged. Policy is fail-closed: unknown tool/role → deny.
- REQ-027: Every tool has: Pydantic input schema, role permission list, timeout, audit events
  (call + result), and test coverage.
- REQ-028: Spawn rate limiting: max live workers per scope and max spawns per minute are
  enforced with a clear error.

### Validation, review, budgets, approvals
- REQ-030: Each task records validation evidence: commands run, exit codes, summary, skipped
  checks, residual risk. Merge gate reads this record (no evidence → no merge).
- REQ-031: A reviewer role (different model family than the author when configured) can be
  required by policy for risky changes; its verdict is an event consumed by the merge gate.
- REQ-032: Per-agent and per-task USD budgets: crossing soft limit emits budget_alert;
  crossing hard limit pauses the agent and opens an approval request.
- REQ-033: Destructive/costly operations (worktree delete, merge, deploy, over-budget
  continue, secrets-adjacent file access) emit approval requests; execution blocks until a
  human decision, which is itself an event.

### Memory
- REQ-040: Project memory stores typed records (architecture_decision, known_bug,
  failed_attempt, command_recipe, dependency_note, deployment_note) separate from the event
  log; compaction (REQ-007) and explicit tool calls write to it; spawn-time prompt building
  retrieves the most relevant records within a fixed token budget.

### Interfaces
- REQ-050: Web dashboard shows: agent roster (status/context%/cost), event timeline with
  filters, per-agent conversation, worktree diff summary, validation evidence, approval queue.
- REQ-051: REST API covers everything the dashboard does (dashboard uses only public API).
- REQ-052 (post-MVP): Telegram bridge mirrors agent conversations to topics and routes user
  replies back; voice notes transcribed.

## Non-Functional Requirements

- Performance: event insert ≤ 5 ms p95; SSE delivery ≤ 200 ms p95; 25 concurrent agents on a
  single host without event loss.
- Reliability: server restart loses zero persisted state; in-flight turns resume or fail to a
  logged error state; SQLite in WAL mode with busy_timeout.
- Security/privacy: internal API token required on every non-dashboard route; agents denied
  read access to secret paths (`.env*`, `~/.ssh`, `~/.aws`, keychains) by tool policy; shell
  tools run with timeout, cwd pinning, env filtering; no secret values in events or prompts.
- Accessibility: dashboard meets WCAG AA (contrast, focus rings, keyboard nav, ARIA on
  icon-only buttons).
- Observability: every agent turn records cost_usd, input/output tokens, context_pct,
  duration; per-scope usage snapshots; structured JSON logs.

## Non-Goals

- Multi-tenant SaaS, RBAC beyond single-operator auth, billing/payments.
- Task-board/PM-platform integrations in the core (may be a plugin later).
- Kubernetes/distributed execution; one host is the unit of deployment.
- Autonomous deployment to production without human approval.

## Acceptance Criteria

- AC-001 (REQ-001/002): POST /api/agents creates an orchestrator; via MCP `spawn_worker` it
  creates a worker visible in GET /api/agents with parent linkage; duplicate name → 409 with
  actionable message.
- AC-003 (REQ-003): kill -9 the server mid-conversation; on restart GET /api/agents shows the
  same roster with statuses; sending a message to a previously-active agent resumes its session.
- AC-004 (REQ-004/021): message to busy agent is delivered exactly once, in order, observable
  as inbox row → delivered event; survives restart while pending.
- AC-007 (REQ-007): agent driven past context threshold compacts automatically; a
  memory record appears; next turn references the summary (assert via MockBackend transcript).
- AC-011 (REQ-011): spawn against a dirty repo returns a structured 409 listing dirty files
  and the four options; no git mutation happened (assert `git status` unchanged).
- AC-013 (REQ-013/030): merge attempt without validation evidence → 422; with evidence and
  clean tree → exactly one squash commit on default branch; conflict case reports files
  without touching default branch.
- AC-014 (REQ-014): second worker claiming overlapping `owned_paths` glob → 409 naming the
  conflicting worker and paths.
- AC-020 (REQ-020): a scripted orchestrator→worker→merge flow produces the exact expected
  event-type sequence (golden test).
- AC-022 (REQ-022): SSE client disconnected for N events reconnects with Last-Event-ID and
  receives all N in order.
- AC-026 (REQ-026/027): tool call from a role without permission → policy_denied tool_result
  event; fuzzed invalid inputs → validation errors, never tracebacks.
- AC-028 (REQ-028): 6th spawn within a minute (limit 5) → rate-limit error event; no session
  row created.
- AC-032 (REQ-032): MockBackend reporting inflated cost crosses hard budget → agent status
  `paused_budget`, approval_request event exists; approving resumes the agent.
- AC-033 (REQ-033): worktree delete via tool requires approval; pending request blocks
  execution; deny → no deletion + logged decision.
- AC-040 (REQ-040): compaction writes ≥1 typed memory record; spawn prompt of a new worker in
  the same project includes the retrieved record within the token budget (assert via prompt
  builder unit test).
- AC-050 (REQ-050): dashboard renders roster/timeline/approvals from a seeded DB with all
  empty/loading/error/success states (Playwright smoke).
