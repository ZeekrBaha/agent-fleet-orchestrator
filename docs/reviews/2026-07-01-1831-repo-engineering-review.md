# Repo Engineering Review — agent-fleet-orchestrator

**Date:** 2026-07-01 18:31 CDT
**Reviewer:** Claude Code (repo-engineering-review skill)
**Tree state:** branch `main`, clean working tree, HEAD `d786fd2`, **14 commits ahead of `origin/main` (unpushed)**

---

## About

`/Users/baha/Desktop/llm-ai-projects/agent-fleet-orchestrator` — Python 3.12 / FastAPI / SQLAlchemy / uv server ("fleet") that spawns, supervises, and merges AI coding agents in isolated git worktrees, with policy manifests, evidence-gated merges, budgets, an approval queue, and an htmx dashboard.

## Verdict

**SHIP-READY with minor docs drift.** All quality gates pass on the live tree: 438 tests green in 14.07s, ruff clean, mypy strict clean across 61 source files, pip-audit reports zero known CVEs. Remaining issues are documentation accuracy (stale status banner, wrong dashboard URL table, undocumented `/api/pipelines`) and the fact that 14 commits — including the entire pipeline feature and the previous review's remediation — have never been pushed to the remote.

## What Was Done Well

- **Full green gate, measured live this session:** `uv run pytest -q` → `438 passed, 18 deselected, 3 warnings in 14.07s`; `ruff check fleet/ tests/` → "All checks passed!"; `mypy fleet/` (strict mode) → "no issues found in 61 source files"; `uvx pip-audit` → "No known vulnerabilities found".
- **CI mirrors the local gate** (`.github/workflows/ci.yml`): ruff → mypy → pytest → pip-audit, on every push and PR. Security audit is an automated gate, not a manual ritual.
- **SQL is parameterized despite f-string appearance.** All four f-string SQL sites (`fleet/events/service.py:124`, `fleet/dashboard/router.py:185`, `fleet/memory/service.py:116`, `fleet/pipeline/repository.py:166`) interpolate only clause fragments built from fixed literal strings (`"kind = :kind"`, `"status = :status"`); every user value goes through bound `:params`. Confirmed by reading each site — no injection path.
- **Docs-drift is tested, not hoped for:** `tests/test_c4_readme_routes.py` parses the README REST API table and asserts every listed route exists in the live app. Rare and valuable pattern.
- **Adversarial/negative test posture:** dedicated files for auth negatives (`test_c2_auth_negative.py`), adversarial inputs (`test_adversarial_inputs.py`), spawn-failure paths (`test_c3_spawn_worker_failure.py`), budget atomicity (`test_p1_2_budget_atomic.py`).
- **Layering holds:** no `fleet/events/`, `fleet/memory/`, `fleet/policy/`, or `fleet/pipeline/` module imports from `fleet.api` or `fleet.dashboard` (rg on import statements — serena not engaged this pass; code unchanged since the prior serena-verified review at d786fd2's parent).
- **README follows the portfolio numbered-section pattern** (Mental model, Why it exists, Quick start, Architecture, Feature walkthrough, REST API, Testing, Ops CLI, Deployment, What's next, Document map, Honesty checklist) and now embeds three dashboard screenshots in §5.
- **TDD-suggestive history:** T1–T13 feature commits each ship models + tests together; test files are named per finding ID (b3–b7, c2–c4, p0–p1), showing test-per-fix discipline. (Commit granularity cannot prove strict test-first; labeled as evidence of test-alongside discipline, not certified TDD.)

## What Was Done Badly

- **[Medium] README status banner is stale** (`README.md:7`): claims "371 tests pass … 54 source files" — actuals are 438 tests / 61 source files. This is the repo's headline claim and it contradicts the correct numbers three lines lower at `README.md:123`. Flagged in the 2026-06-30 review; still present.
- **[Medium] Dashboard URL table is wrong in 4 of 6 rows** (`README.md` §5 "Web dashboard"). Table vs actual routes in `fleet/dashboard/router.py`:
  - Conversation: table `/dashboard/agents/{id}` → actual `/dashboard/agents/{agent_id}/conversation` (router.py:343)
  - Event timeline: table `/dashboard/events` → actual `/dashboard/timeline` (router.py:395)
  - Worktree diff: table `/dashboard/agents/{id}/worktree` → actual `/dashboard/worktrees/{worktree_id}` (router.py:451)
  - Validation: table `/dashboard/agents/{id}/validation` → actual `/dashboard/tasks/{task_id}/validation` (router.py:493)
  Only roster (`/dashboard/`) and approvals (`/dashboard/approvals`) match. Also flagged 2026-06-30; still present.
- **[Medium] `/api/pipelines` is a shipped, undocumented API surface.** `fleet/api/pipelines.py` registers 4 endpoints (POST `/api/pipelines`, POST `/{run_id}/advance`, GET `/{run_id}`, POST `/preview`) — none appear in the README §6 REST API table (rg for "pipelines" in README returns nothing).
- **[Low] The drift-guard has two blind spots** that explain why the above survive a green suite: `test_c4_readme_routes.py` (a) only parses tables whose header starts `| Method |`, so the §5 dashboard table is never checked, and (b) by design only asserts listed-routes-are-accurate, never completeness — so a whole missing router is invisible to it.
- **[Low] 14 commits unpushed** (`git log origin/main..HEAD` = 14). The remote and its CI badge reflect a pre-pipeline, pre-remediation repo. Local-only history is one disk failure away from loss.

## README

Exists, comprehensive: 12 top-level sections covering mental model, architecture, quickstart, keys (live-backend section), repo map (Document map + module layout), tech stack rationale, limitations (§10 What's next), plus an Honesty checklist. Screenshots present in §5 (`docs/screenshots/dashboard-agents.png`, `-timeline.png`, `-approvals.png`). Defects: stale status banner (line 7), wrong dashboard URL table (§5), missing `/api/pipelines` rows (§6). Line 429's "191 tests" reference is to a historical validation report and is acceptable as a dated artifact.

## TDD / Tests

- 438 tests pass, 18 deselected (`live`/`slow` markers), 14.07s, offline via MockBackend fixture replay.
- Coverage spans lifecycle, policy, budget, merge gate, SSE, auth negatives, adversarial inputs, boot smoke, e2e pipeline, plus Playwright dashboard smoke (marker-gated).
- Evidence of test-alongside discipline is strong (per-finding test files, tests shipped in the same commit as features). Strict test-first TDD cannot be certified from squashed task commits — has-tests: yes; TDD-certified: not provable.

## Lint / Type / CI

- ruff (`E,W,F,I,B,UP`): **clean**.
- mypy `strict = true`, Python 3.12: **clean, 61 source files**.
- CI: checkout → uv sync → ruff → mypy → pytest → pip-audit. No gaps for this stack. Coverage reporting is absent (optional enhancement, not a gate failure).
- 3 residual pytest warnings, all upstream deprecations (fastapi testclient httpx, websockets.legacy, uvicorn WebSocketServerProtocol) — no local action available.

## Security / Vulnerabilities

- **Confirmed clean:** `uvx pip-audit` → no known vulnerabilities (post the 2026-06-30 pydantic-settings/starlette bumps).
- **Confirmed safe:** all 4 dynamic-SQL sites use bound parameters; interpolated fragments are compile-time literals.
- **Confirmed:** no `shell=True` / `os.system` in `fleet/`; no hardcoded secrets matched; no `.env` files in tree.
- **Static-only observations:** dashboard HTML views authenticate via `Depends(require_token)` on the router (`fleet/dashboard/router.py:33`) with a `?token=` query-param convention — query-param tokens can leak via logs/referrers; acceptable for a localhost ops dashboard, worth a note in the deployment guide for any non-localhost exposure. Auth negative-path tests exist (`test_c2_auth_negative.py`).

## How To Improve

1. Fix `README.md:7` banner: 371→438 tests, 54→61 source files. One line; it is the repo's headline.
2. Correct the 4 wrong rows in the §5 dashboard URL table to match `fleet/dashboard/router.py`.
3. Add the 4 `/api/pipelines` endpoints to the README §6 table (C4 will then also guard them).
4. Extend `test_c4_readme_routes.py`: (a) parse the dashboard table too, (b) optionally assert completeness for `/api/*` GET/POST routes so a new router can't ship undocumented.
5. `git push origin main` — 14 commits of work exist only on this disk.

## How To Enhance

- Coverage gate in CI (`pytest --cov` + threshold) to catch untested new modules.
- `?token=` note in §9 deployment guide: recommend header-only auth or a session cookie if the dashboard is ever bound beyond localhost.
- Pipeline docs: §5 feature walkthrough has no "Pipelines" subsection even though full-sdlc pipelines are the newest headline feature (T1–T13).
- Consider `gitleaks` in CI for secret-scanning parity with the pip-audit gate.

## Verification

| Check | Command | Result |
|---|---|---|
| Tests | `uv run pytest -q` | 438 passed, 18 deselected, 3 warnings, 14.07s |
| Lint | `uv run ruff check fleet/ tests/` | All checks passed |
| Types | `uv run mypy fleet/` | Success: no issues in 61 source files |
| Dep audit | `uvx pip-audit` | No known vulnerabilities found |
| SQL injection | rg + manual read of 4 f-string sites | Safe — bound params only |
| Layering | rg imports of `fleet.api`/`fleet.dashboard` from lower layers | None found |
| Secrets/shell | rg hardcoded creds, `shell=True`, `.env` | None found |
| Git state | `git status` / `git log origin/main..HEAD` | Clean tree; 14 unpushed commits |

Not run: `live`/`slow` marker suites (need ANTHROPIC_API_KEY / long-running), Playwright smoke (browser-gated; passed in prior session), runtime boot (verified in prior session, code unchanged since).
