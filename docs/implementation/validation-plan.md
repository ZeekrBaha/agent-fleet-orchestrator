# Validation Plan

Date: 2026-06-10
Status: Approved

Definition of done for any task, phase, and the MVP. CI runs the offline set on every push.

## Commands (offline, no secrets — CI mandatory)
- `uv run ruff check .` — lint, zero findings.
- `uv run mypy fleet` — typecheck, zero errors.
- `uv run pytest -q -m "not live and not slow"` — full offline suite.
- `uv run pytest -q -m slow` — load tests (writer throughput, SSE fan-out) — nightly CI lane.

## Commands (live, secrets required — manual/scheduled lane)
- `uv run pytest -q -m live` — Claude adapter smoke (session start/resume, one tool call).

## Functional gates per phase
- Phase 1: config matrix, migration idempotency, writer ordering + zero-loss load test, SSE
  Last-Event-ID catch-up.
- Phase 2: lifecycle state machine, restart restore (kill/restore integration), inbox FIFO
  at-least-once incl. restart with pending rows, budget soft/hard behavior.
- Phase 3: dirty-repo 409 + four options with no repo mutation, ownership overlap rejection,
  WIP golden report, worktree cleanup-on-failure.
- Phase 4: policy fail-closed (unknown role/tool), permission denial events, input fuzzing →
  typed errors never tracebacks, spawn rate limit, secret-path denial.
- Phase 5: prompt layer budgets, missing-role-prompt hard error, golden event-sequence for
  orchestrator→worker→report flow, compaction writes memory + resumes.
- Phase 6: merge blocked without evidence (422), conflict simulation leaves default branch
  untouched (assert SHA), single squash commit, approval block/approve/deny round-trips,
  reviewer verdict consumed by gate.
- Phase 7: Playwright seeded-DB smoke of all six views in all states; backup/restore drill;
  `fleet doctor` finds planted orphans.

## AI behavior coverage (MockBackend in CI; live optional)
- Delegation: canned task → orchestrator spawns expected workers, no overlapping paths.
- Evidence discipline: worker skipping `record_validation` cannot merge.
- Injection resistance: hostile tool_result content cannot cause unauthorized tool call
  (policy denies; event proves attempt).
- Budget compliance: hard-limit pause acknowledged; resume after approval.
- Prompt regression: prompt builder snapshot tests per role (layers, order, budgets).

## Security gate
- Grep gate in CI: no provider key patterns in repo; events scrubber unit tests.
- Tool process runs with no provider secrets in env (assert in integration test).
- Shell tool: timeout kill verified; cwd escape attempt (`../`) rejected.

## Anti-Slop Visual Gate (Phase 7, run in browser against seeded DB)
- Tokens from design-system.md applied: faces are Space Grotesk / IBM Plex Sans / IBM Plex
  Mono (computed styles, not just CSS rules); background `#0F1417`; single accent `#33B6A8`.
- No Inter/Roboto/Arial anywhere; no gradients; no emoji icons (Lucide only).
- Every view shows real seeded data, never placeholder strings.
- All five states reachable and styled per view (toggle via fixtures).
- Keyboard-only pass: roster → conversation → approve flow completable; focus visible
  throughout; axe-core scan zero serious/critical.

## CI / Release Gate
- [ ] Offline suite + lint + typecheck green on every push (GitHub Actions, uv cached).
- [ ] Nightly: slow lane green.
- [ ] MVP tag requires: all phase gates checked, validation-report.md filled, live smoke run
      at least once and recorded.
