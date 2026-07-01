# Repo Engineering Re-Review — agent-fleet-orchestrator

**Date:** 2026-07-01 18:48 CDT
**Reviewer:** Claude Code (repo-engineering-review skill)
**Scope:** Verify fixes claimed after the 2026-07-01-1831 review; re-run all gates on the live tree.
**Tree state:** branch `main`, clean working tree, HEAD `fb13f92`, **fully pushed** (`origin/main` == HEAD).

---

## About

`/Users/baha/Desktop/llm-ai-projects/agent-fleet-orchestrator` — Python 3.12 / FastAPI / SQLAlchemy / uv server that spawns, supervises, and merges AI coding agents in isolated git worktrees, with policy manifests, evidence-gated merges, budgets, approval queue, and an htmx dashboard.

## Verdict

**NOT ship-ready yet — one blocking finding.** All five findings from the 18:31 review were genuinely fixed and pushed in `fb13f92`, and the local gate is fully green (440 passed, ruff clean, mypy strict clean, pip-audit clean). But remote verification exposed something the local gate cannot see: **GitHub Actions CI has never passed on this repository.** Every recorded run — two on 2026-06-11 and the one triggered by today's push — fails on the exact same test, `tests/test_merge_gate_fixes.py::test_p1_12_same_repo_different_scopes_serialize`. The README's "MVP complete and verified" banner sits directly under a red CI badge. Fix that one test (or its environment assumption) and this repo is SHIP.

Secondary: the fix commit added 2 new C4 guard tests, so the suite is now **440** tests — the README's freshly-corrected "438" is stale again in 4 places (`README.md:7`, `:123`, `:188`, `:346`). Hardcoded counts churn every time a test lands; recommend removing exact counts from prose.

## Fix Verification (all 5 prior findings)

| # | Prior finding | Status | Evidence |
|---|---|---|---|
| 1 | Stale banner (371/54) | **FIXED** | `README.md:7` now reads 438 tests / 61 source files (438 already stale again — see below) |
| 2 | Dashboard URL table wrong in 4/6 rows | **FIXED** | §5 table rows now exactly match `fleet/dashboard/router.py:307–542` (all 6 verified) |
| 3 | `/api/pipelines` undocumented | **FIXED** | 4 rows added at `README.md:332–335` matching `fleet/api/pipelines.py:143–193` |
| 4 | C4 guard blind spots | **FIXED** | `tests/test_c4_readme_routes.py` gained `test_readme_dashboard_urls_match_live_app` (line 97) and `test_all_api_routes_documented_in_readme` (line 121) — both blind spots closed |
| 5 | 14 unpushed commits | **FIXED** | `git log origin/main..HEAD` = 0; `origin/main` == `fb13f92` |

## New Findings

### [High — blocking] CI has never been green; one test fails deterministically on ubuntu

- Latest run (push of `fb13f92`): `1 failed, 439 passed` — `FAILED tests/test_merge_gate_fixes.py::test_p1_12_same_repo_different_scopes_serialize - assert False` (where False = `any(<genexpr>)`).
- Runs 27383856053 and 27380242441 (2026-06-11): **identical failure, same test, same assert.**
- Same test passes locally on macOS (part of the 440-green run this session).
- Not random flakiness — 3/3 runs fail identically, so it is an environment-dependent failure (Linux CI runner timing/git behavior vs macOS). The test races two concurrent merges with an `asyncio.sleep(0.01)` ordering assumption (`tests/test_merge_gate_fixes.py:446+`) and asserts at least one is blocked/serialized; on the CI runner neither outcome matches the expected pattern.
- Impact: the CI badge at the top of the README is red while the banner claims "MVP complete and verified"; every future PR gate is dead on arrival; the pip-audit CI gate added on 2026-06-30 has never actually executed a passing run.
- Needs-runtime-verification: whether the merge lock itself misbehaves on Linux, or only the test's timing assumption does. The lock-keyed-by-repo_path behavior it guards (`fleet/review/lock.py` `MergeInProgressError`) is exactly the kind of invariant that must hold on the deployment OS — worth reproducing in a Linux container before assuming "test-only" flake.

### [Low] Test-count drift reintroduced by the drift fix

- `fb13f92` added 2 tests → suite is now 440, README says 438 at lines 7, 123, 188, 346.
- Structural fix: stop hardcoding exact counts in prose; say "the full suite" or auto-generate the number, otherwise every test added re-stales the README.

## Gate Results (live, this session)

| Check | Command | Result |
|---|---|---|
| Tests (local) | `uv run pytest -q` | **440 passed**, 18 deselected, 3 warnings, 13.37s |
| Lint | `uv run ruff check fleet/ tests/` | All checks passed |
| Types | `uv run mypy fleet/` | Success: no issues in 61 source files |
| Dep audit | `uvx pip-audit` | No known vulnerabilities found |
| Tests (CI, ubuntu) | GitHub Actions on `fb13f92` | **1 failed, 439 passed** — p1_12 |
| Push state | `git rev-parse origin/main HEAD` | Identical SHAs |

## How To Improve (ordered)

1. **Fix `test_p1_12_same_repo_different_scopes_serialize` on Linux.** First reproduce in a Linux container (`docker run ... uv run pytest tests/test_merge_gate_fixes.py -k p1_12`) to determine whether the merge lock or the test's sleep-based ordering is at fault. If test-only: replace `asyncio.sleep(0.01)` sequencing with a deterministic synchronization point (an `asyncio.Event` set inside the first merge's critical section). If the lock: that is a product bug on the deployment OS and blocks ship outright.
2. After CI is green, replace hardcoded "438"/"440" test counts in README prose with count-free wording.
3. Optional: add the p1_12 investigation outcome to docs/reviews so the failure archaeology isn't lost.

## How To Enhance

- Run CI on a matrix (`ubuntu-latest`, `macos-latest`) — this exact class of "passes on my Mac" bug is what a matrix catches.
- Add a required-status branch protection once CI is green, so a red gate can't be pushed past silently again.
- Coverage gate and gitleaks (carried over from prior review, still open, still optional).

## Verification

Commands run this session: `git status`/`log`/`rev-parse` (clean, pushed), `uv run pytest -q` (440 green local), `ruff` (clean), `mypy` (clean), `uvx pip-audit` (clean), README §5/§6/banner re-reads against `fleet/dashboard/router.py` and `fleet/api/pipelines.py`, `gh run list` + `gh run view --log-failed` on the 3 recorded CI runs (all fail on p1_12).
Not run: live/slow suites (API-key/long-running), Playwright smoke (browser-gated), Linux-container repro of p1_12 (recommended next step; not executed — review-only pass).

---

## Remediation Addendum (2026-07-01, commit `1bc74c3`)

Both findings fixed and verified the same evening; **verdict upgraded to SHIP**.

**[High] p1_12 CI failure — root-caused and fixed.** Not a Linux timing/git
difference and not a merge-lock bug. An instrumented local reproduction
(parameterized over b-start delays 0/0.01/0.5/2.0s) showed both merges *always*
failed the evidence-staleness gate — `MergeGateError: evidence is stale:
recorded at SHA(s) None` — because the test recorded evidence before any
commits existed. The test passed locally only by accident: merge A still held
the repo lock while failing its own gate when B arrived 10ms later, so B raised
`MergeInProgressError` and satisfied `any("blocked" in r ...)`. On the slower
ubuntu runner, A errored and released the lock before B woke → `[a-error,
b-error]` → `assert False`. Deterministic, which matches 3/3 identical CI
failures. Fix: record evidence after each worktree commit with its real SHA
(the same pattern the other merge-gate tests already use) and add
`assert not errors` so any future failure is diagnosable instead of a bare
`assert False`. Post-fix ordering matrix: concurrent → one merge blocked by the
repo-keyed lock; sequential → both merge cleanly; zero errors. The
serialization invariant is now genuinely exercised — previously the test never
saw a successful merge at all.

**[Low] README count drift — structurally fixed.** All four hardcoded "438"
sites replaced with count-free wording ("full offline suite passes"), so adding
tests can no longer re-stale the banner.

**CI verification:** run 28555679334 on `1bc74c3` → **success** (48s) — the
first green CI run in this repository's history. Local gate at the same
commit: 440 passed / ruff clean / mypy strict clean.
