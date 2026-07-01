# PR #1 Re-validation — Fable Review

Re-validation of [PR #1](https://github.com/ZeekrBaha/agent-fleet-orchestrator/pull/1)
(`fleet/phase-1-scaffold`) against the prior report `docs/reviews/pr-1-fixes-needed.md`.
Verified at branch tip `b8e1c0c` (local == `origin/fleet/phase-1-scaffold`).
Full suite: **349 passed, 18 deselected**. Ruff: **8 errors** (import-sort, new test files only).

## Verdict

**5 of 6 P0/P1 items fully resolved. P0-3 is PARTIAL — a real merge-gate bypass
remains on the primary evidence-recording path.** P2 items untouched (expected;
plan deferred them to a follow-up PR). Recommend fixing the P0-3 gap and the ruff
errors before merge; everything else is in good shape.

## Checklist

| Item | Status | Evidence |
|------|--------|----------|
| P0-1 XSS in dashboard errors | **RESOLVED** | commit `a8e55b3` |
| P0-2 Agent identity binding | **RESOLVED** | commits `13234aa` + prior identity work |
| P0-3 Evidence commit binding | **PARTIAL** | commit `1c3b9b0` — see gap below |
| P1-1 MergeLock multi-process footgun | **RESOLVED** | commit `b8e1c0c` |
| P1-2 Budget check/spend atomicity | **RESOLVED** | commit `0d3c934` |
| P1-3 Audit log append-only | **RESOLVED** | commit `067f702` |
| P2-1 Security/chaos test depth | **NOT RESOLVED** | no new files |
| P2-2 Repo hygiene (CI/LICENSE/etc.) | **NOT RESOLVED** | files absent |

### P0-1 — XSS in dashboard error rendering: RESOLVED

- `html` imported (`fleet/dashboard/router.py:15`); both exception interpolations now
  wrapped in `html.escape(...)` (`fleet/dashboard/router.py:613`, `:618`).
- All other dynamic rendering goes through Jinja templates (auto-escaping); remaining
  literal `HTMLResponse` strings are static text.
- Tests: `tests/test_p0_1_xss_escape.py` — `test_dashboard_error_escapes_html_in_decide_valueerror`
  and `..._db_error` assert `&lt;script&gt;` present, raw `<script>` absent.
- Minor: optional CSP header (recommendation 3) not added. Not a blocker.

### P0-2 — Agent identity binding: RESOLVED

- `require_agent_identity` (`fleet/api/auth.py:128`) resolves identity from the token
  and returns `AgentIdentity`; admin tokens get `is_admin=True`.
- `dispatch_tool` rejects body/token identity mismatch with an explicit
  `identity_mismatch` error (`fleet/api/tools.py:303-304`) and injects
  `_authenticated_agent_id` into handler services (`fleet/api/tools.py:355`).
- All 8 attribution sites in `fleet/api/tool_handlers.py` (lines 132, 193, 302, 316,
  335, 349, 375, 434) now use `svcs.get("_authenticated_agent_id") or inp.agent_id` —
  the body value is only a fallback for admin callers (intended acting-as path).
- Test trio present: `tests/test_identity_binding.py:151`
  (`test_token_for_agent_a_claiming_agent_b_returns_403`), `:209`
  (`test_valid_token_matching_identity_returns_200`), `:285`
  (`test_admin_token_impersonation_emits_event`), plus
  `tests/test_p0_2_identity_attribution.py` (events carry token identity, not body).

### P0-3 — Evidence commit binding: PARTIAL

What's done:
- Migration `fleet/migrations/0005_evidence_commit_sha.sql` adds the column.
- `EvidenceService.record_evidence` accepts and stores `commit_sha`
  (`fleet/review/evidence.py:91`, `:124`).
- `check_merge_gate` rejects stale evidence when `branch_sha` is provided
  (`fleet/review/evidence.py:230-244`), and `MergeService` captures the worktree
  HEAD via `git rev-parse HEAD` and passes it at both gate call sites
  (`fleet/review/merge.py:203-210`, `:325-333`).
- Tests: `tests/test_p0_3_evidence_sha.py` covers record/stale-reject/fresh-accept.

The gap (why PARTIAL):
1. The API recording path never supplies a SHA: `_handle_record_validation`
   (`fleet/api/tool_handlers.py:296-305`) calls `record_evidence` **without**
   `commit_sha`, so every evidence row recorded via the `record_validation` tool
   has `commit_sha = NULL`.
2. The staleness check skips NULL rows (`fleet/review/evidence.py:232`:
   `if e.get("commit_sha") is not None and ...`).
3. Combined: evidence recorded through the real agent-facing path is never checked
   for staleness — the original attack (record green on commit X, push Y, merge Y)
   still works end-to-end. The prior report explicitly required the service to
   capture `git rev-parse HEAD` itself rather than trust the caller; that step
   was not implemented.
4. Secondary: `branch_sha` falls back to `None` on `GitError`
   (`fleet/review/merge.py:206-207`, `:328-329`), silently disabling the
   staleness check (fail-open).

The unit tests pass because they supply `commit_sha` explicitly to the service —
they verify the mechanism, not the wiring.

### P1-1 — MergeLock multi-process footgun: RESOLVED (option 1, minimal/honest)

- Startup guard refuses `WEB_CONCURRENCY > 1` with an explanatory error
  (`fleet/config.py:57-61`); single-process invariant documented in the docstring.
- Tests: `tests/test_p1_1_single_worker.py`.
- Note: this is the report's option 1 (guard), not option 2 (SQLite lock). Acceptable
  per the report's own ordering ("pick one, in order of preference" listed guard first
  as minimal/honest).

### P1-2 — Budget TOCTOU: RESOLVED

- `check_pre_turn` is now an atomic check+reserve inside a single queued write op
  (`fleet/agents/budget.py:58-111`, `_check_and_reserve`); `record_turn_cost`
  reconciles the reservation against actual spend (refund path,
  `fleet/agents/budget.py:118-124`).
- Tests: `tests/test_p1_2_budget_atomic.py` (concurrent turns cannot exceed cap;
  reservation reconciliation).

### P1-3 — Audit log append-only: RESOLVED

- Migration `fleet/migrations/0006_audit_append_only.sql` adds `BEFORE UPDATE` and
  `BEFORE DELETE` triggers on `events` with `RAISE(ABORT, ...)` (lines 10, 16).
- Tests: `tests/test_p1_3_audit_immutable.py` (3 tests, UPDATE/DELETE rejected).
- Optional hash-chain hardening deferred, as the report allowed.

### P2-1 — Security/chaos test depth: NOT RESOLVED

No `test_adversarial_inputs.py`, `test_chaos_recovery.py`, or live-marker smoke test
in `tests/`. Expected — the report's own execution order deferred P2 to a follow-up PR.

### P2-2 — Repo hygiene: NOT RESOLVED

No `.github/workflows/`, `LICENSE`, `CHANGELOG.md`, or `CONTRIBUTING.md` at repo
root. Same deferral as P2-1.

## Remaining blockers (before merge)

1. **P0-3 wiring gap** — make `record_evidence` capture the worktree HEAD itself
   (or have `_handle_record_validation` resolve and pass it), and decide a policy
   for NULL-sha rows at the gate (reject, or require ≥1 SHA-matched pass row).
   Add an end-to-end test through the `record_validation` tool path.
2. **Ruff: 8 errors** (6 auto-fixable, import organization in the new P0/P1 test
   files). Repo convention requires ruff clean before each commit; currently dirty.

## Next steps

1. Fix P0-3 wiring (TDD: red test through the API tool path first), re-run gate.
2. `ruff check --fix` + manual fix for the 2 non-auto-fixable; commit.
3. Consider fail-closed behavior when `git rev-parse` fails at the merge gate.
4. P2-1/P2-2 as the planned follow-up PR (CI workflow first — it also verifies
   "349 green" publicly).
