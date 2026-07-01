# Repo Engineering Review — agent-fleet-orchestrator

**Date:** 2026-06-30 20:15 (local)
**Reviewer:** Claude (repo-engineering-review skill)
**Target:** `/Users/baha/Desktop/llm-ai-projects/agent-fleet-orchestrator` @ `main` (HEAD `18df116`)

---

## About

`fleet` — a FastAPI + SQLAlchemy (SQLite/WAL) Python 3.12 service that orchestrates a fleet of Claude coding agents: spawns them in isolated git worktrees, enforces per-agent budgets and policy manifests, gates merges behind evidence, streams events over SSE, and now runs a multi-stage SDLC pipeline (FULL_SDLC DAG) over a repository. Ships a server-rendered web dashboard and an ops CLI.

## Verdict

**Ship-ready as an MVP, with two non-blocking fixes recommended before a tagged release.** All quality gates are green on the live tree (ruff, mypy --strict, 438 tests). Architecture is cleanly layered with one-way coupling. The only real security finding is **3 known-vulnerable pinned dependencies** (medium; `pydantic-settings`, `starlette`). README is comprehensive but has stale test counts and lacks dashboard screenshots.

---

## What Was Done Well

- **All gates green on live tree** (not from memory — re-run this session):
  - `ruff check fleet/ tests/` → All checks passed.
  - `mypy fleet/` → Success, no issues in 61 source files, **strict mode** (`[tool.mypy] strict = true`).
  - `pytest -q` → **438 passed, 18 deselected** in 13.6s.
- **Constant-time auth.** `fleet/api/auth.py` uses `hmac.compare_digest` for token comparison; supports Bearer header + `?token=` query (for `EventSource`). Loopback bypass is scoped to *empty token AND loopback client*, and `fleet/config.py:50` fails startup if a non-local bind has no token. Sound fail-closed posture.
- **No shell injection surface.** `fleet/workspace/gitops.py` runs git via `subprocess.run(..., shell=False)` with list args and a timeout; pure wrappers, no business logic. `GitError` wraps non-zero/timeout.
- **SQL is parametrized.** Every query uses bound `:params`. The two dynamic-clause builders (`fleet/pipeline/repository.py:166` `SET {set_clause}`, `fleet/events/service.py:124` `WHERE {where}`) assemble clauses from **hardcoded string literals only** — user values always flow through bindings. No injection.
- **Clean layering / AI-ready.** `rg` confirms no service or `db.py` module imports `fleet.api` — dependency flows one way (api → services → db). Module tree mirrors the conceptual architecture; public APIs are typed and docstringed (e.g. gitops docstring lists its public surface). Good progressive disclosure.
- **Strong TDD signal.** `.superpowers/sdd/progress.md` documents RED→GREEN per task and records real bugs caught during TDD (UNIQUE(scope,name) root-agent collision in T6; spec-evolution test fix in T8). Tests are co-committed with impl (e.g. T3 `7631c58`: repository.py + migration + test_repository.py in one commit, 365 insertions). "Has tests" and "shows TDD discipline" both hold here.
- **Comprehensive test taxonomy.** 65 test files spanning P0/P1/P2 security (`test_p0_*`, xss escape, identity attribution, evidence sha), chaos/recovery, adversarial inputs, concurrency, auth-negative, boot/dashboard smoke, and the full pipeline DAG e2e.
- **CI enforces the real gates** (`.github/workflows/ci.yml`): ruff → mypy → pytest on every push/PR.

## What Was Done Badly

- **[Medium/security] Vulnerable pinned dependencies.** `uv run pip-audit` (this session):
  - `pydantic-settings 2.14.1` → GHSA-4xgf-cpjx-pc3j (fix 2.14.2)
  - `starlette 1.2.1` → PYSEC-2026-249 (fix 1.3.1), PYSEC-2026-248 (fix 1.3.0)
  Lockfile pins vulnerable versions. Not exploited by inspection, but should be bumped. No `pip-audit` step in CI to catch the next one.
- **[Low/drift] Stale test counts in README.** README §3/§7 claim "191 passed, 13 deselected"; live suite is **438 passed, 18 deselected**. Suite more than doubled (pipeline work) without doc sync.
- **[Low/UI] No dashboard screenshots.** Repo ships a web dashboard (`fleet/dashboard/router.py`, README §5 "Web dashboard") but README has no `## Screenshots` section. Review standard requires screenshots for dashboard/UI repos.
- **[Polish] Deprecated uv config.** `pyproject.toml` uses `[tool.uv] dev-dependencies`, which emits a deprecation warning on every uv invocation; migrate to `[dependency-groups] dev`.
- **[Polish] Review-doc clutter in working tree.** 6 untracked markdown files under `docs/reviews/` (`pr-1-fable-revalidation*.md`, `pr-1-fixes-needed.md`, `p2-fix-plan.md`) mixed with 2 tracked ones — decide tracked-or-ignored.
- **[Info] Pipeline-consolidation work landed on `main`.** T1–T13 committed directly to `main` (per progress ledger). Functional and gated, but a feature branch + PR would match the repo's own evidence-gated-merge ethos.

## README

Exists, **comprehensive** — 10 numbered sections + Document map + Honesty checklist: mental model (§1), why (§2), quickstart (§3), architecture + key design decisions (§4), feature walkthrough (§5), REST API (§6), testing (§7), ops CLI (§8), production deployment (§9), what's next (§10). Covers keys (`ANTHROPIC_API_KEY`, `FLEET_API_TOKEN`), repo/module map, tech stack, limitations. **Gaps:** stale test counts (191 vs 438); no `## Screenshots` section for the dashboard.

## TDD / Tests

Real TDD evidence, not just test presence. Progress ledger narrates RED→GREEN and documents bugs found *during* the cycle. Tests co-committed with implementation. 65 test files, 438 passing, security/chaos/adversarial/concurrency coverage plus full pipeline e2e. `[tool.pytest] addopts = -m 'not live and not slow'` keeps the default suite hermetic (MockBackend, no API key).

**Commands run:** `uv run pytest -q` → 438 passed, 18 deselected, 2 deprecation warnings (websockets legacy), 13.6s.

## Lint / Type / CI

- **ruff** (`select E,W,F,I,B,UP`): `uv run ruff check fleet/ tests/` → All checks passed.
- **mypy strict**: `uv run mypy fleet/` → Success, 61 files, zero issues.
- **CI**: `.github/workflows/ci.yml` runs all three gates on push + PR (Python 3.12, `astral-sh/setup-uv@v4`).
- **Missing gate:** no dependency-audit step (`pip-audit`) in CI.

## Security / Vulnerabilities

**Confirmed:**
- 3 known CVEs in pinned deps (see above) — `pydantic-settings 2.14.1`, `starlette 1.2.1`. Medium. Bump + relock.

**Verified-safe (static):**
- Token comparison constant-time (`hmac.compare_digest`).
- Loopback auth bypass correctly scoped; non-local bind without token fails at startup (`config.py:50`).
- git subprocess `shell=False`, list-arg, timeouted.
- SQL fully parametrized; dynamic clauses from hardcoded literals only — no injection.
- Dedicated P0/P1 security tests (xss escape, identity attribution/binding, evidence sha, audit immutability, budget atomicity, sse auth).

**Needs runtime verification:** worktree path handling and merge simulation under adversarial repo state (covered by chaos tests but not exhaustively fuzzed).

## How To Improve

1. **Bump vulnerable deps** and relock: `pydantic-settings>=2.14.2`, `starlette>=1.3.1` (via `fastapi`); `uv lock && uv run pip-audit` to confirm clean.
2. **Add a `pip-audit` step to CI** so dependency CVEs fail the build going forward.
3. **Sync README test counts** to 438/18 (§3, §7) — or, better, stop hardcoding counts.
4. **Add `## Screenshots`** to README with dashboard captures (boot server, hit `/dashboard`, screenshot key screens).
5. **Migrate `pyproject.toml`** from `[tool.uv] dev-dependencies` to `[dependency-groups] dev` to kill the deprecation warning.
6. **Resolve `docs/reviews/` clutter** — track the keepers, gitignore the scratch.

## How To Enhance

- **PR-based flow for feature work.** The product enforces evidence-gated merges for its agents; apply the same to its own `main` (feature branch → CI → review → merge).
- **Observability.** Structured logging + request IDs across the api→service→db path; expose pipeline stage transitions as metrics, not just SSE.
- **Dependency automation.** Dependabot/Renovate to keep the lockfile ahead of CVEs.
- **Runtime security depth.** Fuzz worktree/branch names and merge-conflict paths; add a rate limit / lockout to the token auth path.
- **Dashboard tests.** Promote the Playwright dashboard smoke into CI (currently browser-install gated) behind a cached browser.

## Verification

| Check | Command | Result |
|---|---|---|
| Lint | `uv run ruff check fleet/ tests/` | ✅ All checks passed |
| Types | `uv run mypy fleet/` | ✅ Success, 61 files, strict |
| Tests | `uv run pytest -q` | ✅ 438 passed, 18 deselected, 13.6s |
| Dep audit | `uv run pip-audit` | ⚠️ 3 vulns in 2 packages (pydantic-settings, starlette) |
| Coupling | `rg 'from fleet.api' <service modules>` | ✅ None — clean downward coupling |
| SQL injection | manual review of dynamic-clause builders | ✅ literals-only, params bound |
| Subprocess | review of `gitops.py` | ✅ shell=False, list args, timeout |

Skipped: `live`/`slow` markers (require `ANTHROPIC_API_KEY`); Playwright dashboard smoke (browser install); dashboard screenshots (would require booting server — noted as missing, not captured, since this is a review not a fix).

## Saved Observations

`docs/reviews/2026-06-30-2015-repo-engineering-review.md` (this file).
