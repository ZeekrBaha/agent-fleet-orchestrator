# PR #1 Re-validation — Fable Final Report

Final re-validation of [PR #1](https://github.com/ZeekrBaha/agent-fleet-orchestrator/pull/1)
(`fleet/phase-1-scaffold`) against the blockers listed in
`docs/reviews/pr-1-fable-revalidation.md`.

Verified at branch tip **`1a4627f`** — confirmed identical across local HEAD,
`origin/fleet/phase-1-scaffold`, and the PR head (`gh pr view 1`). PR state: OPEN.

Validation run on this machine (2026-06-11):

- `ruff check .` → **All checks passed!**
- `mypy fleet` → **Success: no issues found in 54 source files**
- `pytest -q` → **351 passed, 18 deselected** (14.71s)
- Focused merge-gate/P0-3 suites (`test_p0_3_wiring.py`, `test_p0_3_evidence_sha.py`,
  `test_merge_gate.py`, `test_merge_gate_fixes.py`) → **32 passed**

## Verdict

**All prior blockers are resolved. APPROVE — PR #1 is ready to merge.**

The two items the prior report flagged as merge blockers (P0-3 wiring gap and
8 ruff errors) are both fixed at `1a4627f`, with end-to-end test coverage through
the real `record_validation` tool path. P2 items remain deferred to a follow-up
PR, as planned.

## Checklist

| Item | Status | Evidence |
|------|--------|----------|
| P0-3a Tool path never supplied SHA | **RESOLVED** | `fleet/api/tool_handlers.py:299-333` |
| P0-3b NULL-SHA rows skipped staleness check | **RESOLVED** | `fleet/review/evidence.py:229-244` |
| P0-3c Fail-open on `GitError` at gate | **RESOLVED** | `fleet/review/merge.py:206-212`, `:330-337` |
| P0-3d E2E test through tool path | **RESOLVED** | `tests/test_p0_3_wiring.py:202`, `:241` |
| Ruff: 8 errors | **RESOLVED** | `ruff check .` → clean at `1a4627f` |
| P0-1, P0-2, P1-1, P1-2, P1-3 | **RESOLVED** (unchanged) | per prior report; suite still green |
| P2-1 Security/chaos test depth | **NOT RESOLVED** (deferred) | follow-up PR, per plan |
| P2-2 Repo hygiene (CI/LICENSE) | **NOT RESOLVED** (deferred) | follow-up PR, per plan |

## Blocker-by-blocker detail

### P0-3a — `record_validation` tool path now stamps a server-resolved SHA

`_handle_record_validation` (`fleet/api/tool_handlers.py:299-322`) resolves the
calling agent's worktree via `worktree_svc.get_worktree(calling_agent.worktree_id)`
and runs `git rev-parse HEAD` in that worktree (thread-pool offload), then passes
the result as `commit_sha` to `record_evidence` (line 333).

Crucially, `RecordValidationInput` (`fleet/api/tool_schemas.py:68-75`) has **no
`commit_sha` field** — agents cannot supply or forge the SHA; it is resolved
entirely server-side, exactly as the original report required.

If SHA resolution fails (`GitError`, missing worktree), `commit_sha` degrades to
`None` — which is safe because of P0-3b below: NULL rows now block the gate
rather than bypass it.

### P0-3b — NULL-SHA evidence treated as stale

The staleness filter in `check_merge_gate` (`fleet/review/evidence.py:232-235`)
changed from skipping NULL rows to:

```python
stale = [e for e in evidence if e.get("commit_sha") != branch_sha]
```

`None != branch_sha` is always true, so any evidence row without a bound SHA
blocks merge whenever the branch tip is known. The bypass — record green
evidence with NULL SHA, then merge unrelated commits — is closed. The docstring
(lines 205-206, 230-231) documents the policy.

### P0-3c — Merge gate fails closed when branch SHA cannot be determined

Both call sites no longer fall back to `branch_sha = None` on `GitError`:

- `check_gate` (dry-run): returns `GateStatus(can_merge=False, reason="cannot
  determine branch SHA for staleness check: ...")` (`fleet/review/merge.py:206-212`).
- `_do_merge` (execution): raises `MergeGateError` with the same reason
  (`fleet/review/merge.py:334-337`).

### P0-3d — End-to-end coverage through the real tool path

`tests/test_p0_3_wiring.py` adds the two tests the prior report demanded:

- `test_record_validation_tool_stamps_commit_sha` (line 241): drives the
  `record_validation` tool through FastAPI/httpx with a real DB and worktree,
  asserts the stored evidence row carries the worktree's actual HEAD SHA.
- `test_null_sha_evidence_blocked_when_branch_sha_known` (line 202): asserts
  NULL-SHA evidence is rejected by the gate when `branch_sha` is supplied.

Pre-existing merge-gate tests were updated to record evidence at the real
worktree HEAD (they previously recorded before committing); all 32 tests in the
four gate/P0-3 files pass.

### Ruff cleanup

Prior report: 8 errors (import-sort + unused imports in new test files).
Current: `ruff check .` reports **All checks passed!** at `1a4627f`.

### Verification claims

The commit message for `1a4627f` and the fix-session notes claimed
"351 passed, ruff + mypy clean." Independently re-run here: all three claims
reproduce exactly (351 passed / ruff clean / mypy clean on 54 files).

## Remaining blockers

**None for merge.**

Non-blocking observations for the follow-up PR:

1. P2-1 (adversarial/chaos test depth) and P2-2 (CI workflow, LICENSE,
   CHANGELOG, CONTRIBUTING) remain open by design. CI first — it makes the
   "suite green" claim publicly verifiable.
2. `_handle_record_validation` silently degrades to `commit_sha=None` on
   `GitError` (`fleet/api/tool_handlers.py:321-322`). Downstream gate behavior
   makes this fail-closed, but a logged warning at record time would aid
   debugging when an agent's evidence is later rejected as unbound.

## Final merge recommendation

**MERGE.** All P0/P1 blockers from both prior reports are resolved and
independently verified at the PR head commit `1a4627f`: the evidence-SHA chain
is now intact end-to-end (server-side resolution → NULL-stale policy →
fail-closed gate), test coverage exercises the real tool path, and the quality
gates (ruff, mypy, 351-test suite) are clean. Follow up with the planned P2 PR
(CI/LICENSE/chaos tests) after merge.
