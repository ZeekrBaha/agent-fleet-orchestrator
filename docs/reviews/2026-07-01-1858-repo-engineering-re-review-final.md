# Repo Engineering Re-Review #3 (Final) — agent-fleet-orchestrator

**Date:** 2026-07-01 18:58 CDT
**Reviewer:** Claude Code (repo-engineering-review skill)
**Scope:** Verify remediation of the 18:48 re-review's two findings (blocking p1_12 CI failure, README count drift); re-run all gates live.
**Tree state:** branch `main`, clean working tree, HEAD `b2a694d`, fully pushed (`origin/main` == HEAD).

---

## About

`/Users/baha/Desktop/llm-ai-projects/agent-fleet-orchestrator` — Python 3.12 / FastAPI / SQLAlchemy / uv server that spawns, supervises, and merges AI coding agents in isolated git worktrees, with policy manifests, evidence-gated merges, budgets, approval queue, and an htmx dashboard.

## Verdict

**SHIP.** Both findings from the 18:48 re-review are fixed and verified against live state, including the remote. GitHub Actions is green on `1bc74c3` and `b2a694d` — **the first successful CI runs in this repository's history**. Local gate fully green: 440 tests, ruff clean, mypy strict clean (61 files), pip-audit zero CVEs. No new findings.

## Fix Verification

### [High → FIXED, remotely verified] p1_12 CI failure

- Fix commit `1bc74c3` ("p1_12 evidence recorded without commit SHA — CI red since first run").
- Root cause per commit + addendum: the test recorded evidence with `commit_sha=None` (before any commits existed), so both merges always failed the evidence-staleness gate; the local pass was accidental (merge A still held the repo lock when B arrived 10ms later → B raised `MergeInProgressError` and satisfied the assert). On the slower ubuntu runner A released the lock first → both errored → `assert False`. Matches the 3/3 identical historical CI failures exactly.
- Fix: evidence now recorded after real worktree commits with their actual SHAs (same pattern as the other merge-gate tests in `tests/test_merge_gate_fixes.py`), plus a diagnosability guard so future failures name the error instead of a bare `assert False`.
- **Decisive evidence:** GitHub Actions `conclusion: success` on both `1bc74c3` and `b2a694d` — the failure was ubuntu-specific, so a green ubuntu run is the ground-truth verification, not a local pass. This diagnosis supersedes the 18:48 review's "Linux timing" hypothesis: the bug was an always-broken test masked by lock timing, not an OS behavioral difference.

### [Low → FIXED] README hardcoded test counts

- `rg -n '438|440' README.md` → zero matches. All four previously-stale count references removed/reworded. Count drift can no longer recur structurally.

## Gate Results (live, this session)

| Check | Command | Result |
|---|---|---|
| Tests (local) | `uv run pytest -q` | 440 passed, 18 deselected, 3 warnings, 13.56s |
| Lint | `uv run ruff check fleet/ tests/` | All checks passed |
| Types | `uv run mypy fleet/` | Success: no issues in 61 source files |
| Dep audit | `uvx pip-audit` | No known vulnerabilities found |
| CI (ubuntu, remote) | GitHub Actions on `1bc74c3`, `b2a694d` | **success** — first green runs ever |
| Push state | `git log origin/main..HEAD` | 0 unpushed; clean tree |

## Remaining Optional Enhancements (carried, non-blocking)

- CI OS matrix (`ubuntu-latest` + `macos-latest`) — the p1_12 saga is the argument for it.
- Branch protection requiring the CI status once workflows stay green.
- Coverage gate (`pytest --cov` + threshold) and `gitleaks` secret-scan step in CI.

## Verification

Commands run this session: `git status`/`log`/`fetch`/`rev-parse` (clean, pushed, HEAD `b2a694d`), `uv run pytest -q` (440 green), `ruff` (clean), `mypy` (clean), `uvx pip-audit` (clean), `git show 1bc74c3 --stat` + test-file rg (fix content confirmed), `rg '438|440' README.md` (empty), `gh run list` (both latest runs success).
Not run: live/slow suites (API-key/long-running), Playwright smoke (browser-gated). Both unchanged by this remediation and previously verified.

## Review Trail

1. `2026-06-30-2015-repo-engineering-review.md` — initial review, 6 findings.
2. `2026-07-01-1831-repo-engineering-review.md` — re-review, 5 findings (docs drift, unpushed work).
3. `2026-07-01-1848-repo-engineering-re-review.md` — fixes verified; discovered CI-never-green blocker (p1_12); remediation addendum appended after `1bc74c3`.
4. This document — final verification: all findings closed, CI green remotely, verdict SHIP.
