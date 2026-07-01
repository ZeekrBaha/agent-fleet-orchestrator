# PR #1 Re-validation #2 — Fable Review

Second re-validation of [PR #1](https://github.com/ZeekrBaha/agent-fleet-orchestrator/pull/1)
(`fleet/phase-1-scaffold`), following up on `docs/reviews/pr-1-fable-revalidation.md`,
which flagged P0-3 as PARTIAL and 8 ruff errors as blockers, with P2-1/P2-2 deferred.

Verified 2026-06-11 6:21 PM CDT at branch tip `3686fbd` (`feat(P2): adversarial/chaos tests, CI
workflow, repo hygiene, SHA warning log`). Local HEAD == `origin/fleet/phase-1-scaffold`
(both `3686fbdbd3ac9b48206994b2e9ab19c4a488bb7b`) — two commits past the prior
report's baseline (`b8e1c0c`), including the P0-3 fix (`1a4627f`) and the P2 commit.

## Verdict

**All blockers from the prior Fable report are resolved, and both deferred P2 items
landed as well.** The P0-3 wiring gap is closed on the real agent-facing
`record_validation` tool path, NULL-SHA evidence fails closed at the merge gate,
branch-SHA resolution failure fails closed, ruff/mypy are clean, and the suite is
371 green. **Ready to merge.**

## Checklist (prior blockers)

| Prior blocker | Status | Evidence |
|---|---|---|
| P0-3a: `_handle_record_validation` never passed `commit_sha` (every API-path evidence row NULL) | **RESOLVED** | `fleet/api/tool_handlers.py:302-343` |
| P0-3b: staleness check skipped NULL-sha rows (gate bypass) | **RESOLVED** | `fleet/review/evidence.py:227-244` |
| P0-3c (secondary): `branch_sha` fell back to fail-open on `GitError` | **RESOLVED** | `fleet/review/merge.py:200-215`, `:326-342` |
| End-to-end test through the `record_validation` tool path | **RESOLVED** | `tests/test_p0_3_wiring.py:202`, `:241`; `tests/test_p0_3_evidence_sha.py` |
| Ruff: 8 errors (import sort + line length) | **RESOLVED** | `ruff check .` → "All checks passed!" at tip |
| P2-1 security/chaos test depth | **RESOLVED** | `tests/test_adversarial_inputs.py` (14 tests), `tests/test_chaos_recovery.py` (6 tests) |
| P2-2 repo hygiene (CI/LICENSE/etc.) | **RESOLVED** | `.github/workflows/ci.yml`, `LICENSE`, `CHANGELOG.md`, `CONTRIBUTING.md` |

## Evidence detail

### 1. Server-side SHA stamping on the real tool path — RESOLVED

`_handle_record_validation` (`fleet/api/tool_handlers.py:290-344`) resolves the
calling agent's worktree HEAD itself:

- Looks up `calling_agent.worktree_id` → `worktree_svc.get_worktree(...)` → runs
  `git rev-parse HEAD` in the worktree path via a thread-pool executor
  (`tool_handlers.py:304-323`), and passes the result as `commit_sha` to
  `record_evidence` (`:343`).
- The SHA is **not caller-supplied**: `RecordValidationInput`
  (`fleet/api/tool_schemas.py:68-75`) has no `commit_sha` field, so an agent
  cannot forge it. The prior report's required design ("the service captures
  `git rev-parse HEAD` itself") is implemented.
- On resolution failure the handler catches `(GitError, OSError)` — broadened at
  `3686fbd` to cover removed worktree directories — logs a warning
  (`tool_handlers.py:324-332`) and stamps `commit_sha = None`. Safe, because
  NULL rows are rejected at the gate (§2): recording degrades gracefully,
  merging stays blocked.

### 2. NULL-SHA gate policy — RESOLVED (fail-closed)

`EvidenceService.check_merge_gate` (`fleet/review/evidence.py:227-244`): when
`branch_sha` is provided, **every** evidence row with `commit_sha != branch_sha`
is stale — explicitly including NULL rows, per the inline comment ("NULL
commit_sha means the evidence was recorded without a known SHA — treat it as
unbound/stale when the branch tip is known"). The original attack (record green
at commit X, push Y, merge Y) and its NULL-sha variant (record with no SHA,
merge anything) are both blocked.

Minor nit (non-blocking): the docstring at `evidence.py:205-206` still says
"all evidence rows with a **non-null** commit_sha must match branch_sha" — the
implementation is stricter than the docstring. Worth a one-line docs fix in a
follow-up; the behavior itself is correct and tested.

### 3. Branch-SHA resolution fails closed — RESOLVED

Both merge-gate call sites refuse instead of silently disabling staleness:

- `check_gate` (dry run): `GitError` on `git rev-parse HEAD` →
  `GateStatus(can_merge=False, reason="cannot determine branch SHA for
  staleness check: ...")` (`fleet/review/merge.py:206-212`).
- `_do_merge` (execution): same failure → raises `MergeGateError`
  (`fleet/review/merge.py:334-337`).

### 4. End-to-end tests through the tool path — RESOLVED

- `tests/test_p0_3_wiring.py:202` —
  `test_null_sha_evidence_blocked_when_branch_sha_known`: NULL-sha evidence no
  longer passes the gate.
- `tests/test_p0_3_wiring.py:241` —
  `test_record_validation_tool_stamps_commit_sha`: exercises the actual
  `record_validation` dispatch path and asserts the stored row carries the
  worktree HEAD SHA.
- `tests/test_p0_3_evidence_sha.py` — three service-level tests: SHA recorded,
  stale evidence rejected, fresh evidence accepted.

### 5. P2-1 — adversarial/chaos test depth — RESOLVED (new since prior report)

- `tests/test_adversarial_inputs.py` — 14 tests (injection-shaped inputs,
  oversized payloads, malformed identifiers, etc.).
- `tests/test_chaos_recovery.py` — 6 failure-injection tests, including the
  removed-worktree path that surfaced the `OSError` gap fixed at `3686fbd`.

### 6. P2-2 — repo hygiene — RESOLVED (new since prior report)

`.github/workflows/ci.yml` (ruff + mypy + pytest in CI), `LICENSE` (MIT),
`CHANGELOG.md`, `CONTRIBUTING.md`, README architecture/status updates.

## Validation commands run (this session, at `3686fbd`)

| Command | Result |
|---|---|
| `uv run ruff check .` | All checks passed! |
| `uv run mypy fleet/` | Success: no issues found in 54 source files |
| `uv run pytest -q` | **371 passed**, 18 deselected, 15.03s (was 351 at `1a4627f`, 349 at prior report) |
| Focused: `test_p0_3_wiring.py test_p0_3_evidence_sha.py` | 5 passed |

## Remaining P0/P1 risk reassessment (from `pr-1-fixes-needed.md`)

P0-1 (XSS), P0-2 (identity binding), P1-1 (`WEB_CONCURRENCY` guard, `b8e1c0c`),
P1-2 (atomic budget reserve, `0d3c934`), P1-3 (append-only event triggers,
`067f702`) — all confirmed RESOLVED in the prior report; their commits remain in
the branch history unchanged and their dedicated test files are in the 371-green
suite. No regressions observed. No new P0/P1 findings at this tip.

Non-blocking observations:

- `tool.uv.dev-dependencies` in `pyproject.toml` is deprecated by uv; migrate to
  `dependency-groups.dev` whenever convenient.
- `check_merge_gate` docstring understates the NULL-SHA policy (see §2 nit).

## Remaining blockers

**None.** Both blockers from the prior report (P0-3 wiring gap, 8 ruff errors)
are fixed and verified live; the deferred P2 items also landed.

## Final merge recommendation

**APPROVE — merge PR #1.** All P0/P1 findings from `pr-1-fixes-needed.md`, both
blockers from the first re-validation, and both previously deferred P2 items are
resolved and verified live at branch tip `3686fbd` with ruff, mypy, and a
371-test suite all green. Nothing blocks merge.

## Next steps

1. Merge PR #1 (CI at `.github/workflows/ci.yml` will re-verify ruff/mypy/pytest).
2. Follow-up (non-blocking): fix `check_merge_gate` docstring at
   `fleet/review/evidence.py:205-206` to state the NULL-SHA-is-stale policy;
   migrate `tool.uv.dev-dependencies` → `dependency-groups.dev` in `pyproject.toml`.
