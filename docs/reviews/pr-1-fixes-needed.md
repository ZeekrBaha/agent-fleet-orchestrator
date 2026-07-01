# PR #1 Review — Fixes Needed

Source: read-only senior engineering review of PR #1 (`fleet/phase-1-scaffold`, ~28k lines, 7 phases, 334 tests green). All findings below were spot-verified against source at review time. Prioritized P0 (fix before merge/deploy), P1 (fix soon after, before any multi-user or multi-process use), P2 (quality/positioning).

---

## P0 — Fix before merge or any non-local deployment

### P0-1. XSS in dashboard error rendering

**Where:** `fleet/dashboard/router.py:612` and `:617` — raw exception text interpolated into an `HTMLResponse` f-string. No `html.escape` anywhere in the file.

**Why it matters:** Exception messages can contain attacker-influenced input (task descriptions, agent names, file paths flow into errors). Rendering them unescaped into HTML is a classic reflected XSS. The dashboard is the operator's trusted surface — script execution there means session/token theft for the highest-privilege user.

**Fix approach:**
1. Import `html` and wrap every dynamic value rendered into HTML with `html.escape(...)` — at minimum the two exception interpolations at lines 612/617.
2. Better: add a small `render_error(exc: Exception) -> HTMLResponse` helper that escapes once, and audit the rest of `router.py` for any other f-string HTML interpolation of dynamic data (task titles, agent ids, event payloads).
3. Consider a `Content-Security-Policy` header on dashboard responses as defense-in-depth (`default-src 'self'`).

**Tests to add (TDD — write red first):**
- `test_dashboard_error_escapes_html`: force a handler to raise an exception whose message contains `<script>alert(1)</script>`; assert response body contains `&lt;script&gt;` and does NOT contain `<script>`.
- Same pattern for any other dynamic field rendered (task title with HTML payload round-tripped through the dashboard page).

---

### P0-2. Agent identity binding incomplete — caller-supplied `agent_id`

**Where:** `fleet/api/tool_handlers.py` (agent_id taken from request bodies), `fleet/api/auth.py` (admin token bypasses per-agent scoping).

**Why it matters:** RBAC is fail-closed (good), but a worker holding its own token can pass another agent's `agent_id` in a tool body and act as that agent — spend its budget, write its events, touch its worktree. The trust kernel only works if identity comes from the credential, not the payload.

**Fix approach:**
1. In `require_agent_identity` (auth layer), resolve the authenticated agent id from the token and attach it to the request (e.g. `request.state.agent_id` or a dependency-injected `AgentIdentity`).
2. In every tool handler, ignore/reject body-supplied `agent_id` when the caller is an agent token: either strip it and use the authenticated identity, or return 403 on mismatch (explicit mismatch rejection is more debuggable — recommended).
3. Keep admin-token override, but make it explicit: admin may act on behalf of any agent, and every such call must write an audit event recording `acting_as`.

**Tests to add:**
- `test_agent_token_cannot_spoof_other_agent`: agent A's token + agent B's `agent_id` in body → 403.
- `test_agent_token_agent_id_inferred`: agent A's token with no/own `agent_id` → succeeds, effects attributed to A.
- `test_admin_acting_as_writes_audit_event`: admin call with explicit `agent_id` → succeeds and audit log records the impersonation.

---

### P0-3. Evidence gate accepts stale evidence — no commit binding

**Where:** `fleet/review/evidence.py` — evidence table has no `commit_sha` column (verified by grep: no `sha` reference in the file).

**Why it matters:** The merge gate's whole premise is "tests passed on the code being merged." Without binding evidence to a commit SHA, an agent can record green evidence on commit X, push commit Y, and merge Y under X's evidence. That's a full bypass of the gate — the central safety mechanism of the orchestrator.

**Fix approach:**
1. Add `commit_sha TEXT NOT NULL` to the evidence table via the migration framework added in Phase B1.
2. Capture `git rev-parse HEAD` of the worktree at evidence-recording time (the evidence service should capture it itself, not trust the caller).
3. At merge time, compare evidence `commit_sha` against the branch tip being merged; reject with a clear error (`EvidenceStaleError` → 409, consistent with `MergeInProgressError` handling from C1) on mismatch.
4. Optionally add a max-age TTL on evidence as a second safety net.

**Tests to add:**
- `test_evidence_records_commit_sha`: recording evidence stores the worktree HEAD SHA.
- `test_merge_rejects_stale_evidence`: record evidence, add a new commit to the branch, attempt merge → 409 + no merge performed.
- `test_merge_accepts_fresh_evidence`: evidence SHA == branch tip → merge proceeds.

---

## P1 — Fix before multi-process or production-shaped use

### P1-1. MergeLock is in-memory only

**Where:** `fleet/review/lock.py:31,44` — `asyncio.Lock` held in an in-process dict.

**Why it matters:** Correct for the current single-process design (and ADR-documented), but silently wrong the moment anyone runs two uvicorn workers or a second process: two merges can interleave on the same repo. This is a footgun with no guardrail.

**Fix approach (pick one, in order of preference):**
1. **Minimal/honest:** enforce the constraint — refuse to start with `workers > 1` (assert at startup/lifespan) and document the single-process invariant in the deploy guide. Cheap, removes the silent-corruption mode.
2. **Real fix:** move the lock into SQLite — a `merge_locks` table with `INSERT ... ON CONFLICT` acquire and TTL/heartbeat expiry, since SQLite is already the single source of truth and WAL handles cross-process access.
3. File-level `flock` on the repo dir is a middle option but adds platform variance; SQLite approach reuses existing infra.

**Tests to add:**
- For option 1: `test_startup_rejects_multi_worker_config`.
- For option 2: two separate connections/processes attempt acquire → exactly one succeeds; expired lock (TTL) becomes acquirable; release by non-holder rejected.

### P1-2. Budget check/spend is not atomic

**Where:** `fleet/agents/budget.py:55` (`check_pre_turn`) called from `fleet/agents/session.py:243` — read budget, then spend, as separate steps.

**Why it matters:** Two concurrent turns for the same agent can both pass `check_pre_turn` against the same remaining balance, then both spend — overshooting the cap. Budgets are an enforcement boundary, not telemetry; TOCTOU here defeats the B5 pre-turn gate.

**Fix approach:**
1. Make reserve-and-check a single SQL statement: `UPDATE budgets SET spent = spent + :reserve WHERE agent_id = :id AND spent + :reserve <= cap` and treat 0 rows updated as denial. Reconcile actual usage after the turn (refund the unused part of the reservation).
2. Since all writes already funnel through the single-writer asyncio queue, the simpler variant is to perform check+reserve inside one queued write operation — same effect, no schema change.

**Tests to add:**
- `test_concurrent_turns_cannot_exceed_cap`: budget with room for one turn; fire two concurrent turn starts (`asyncio.gather`); assert exactly one proceeds and final spent ≤ cap.
- `test_reservation_refunded_on_cheap_turn`: reserve > actual usage → balance reflects actual.

### P1-3. Audit log has no append-only enforcement

**Where:** audit log table (infrastructure layer) — append-only is convention only; nothing stops `UPDATE`/`DELETE`.

**Why it matters:** The audit log is the forensic record for an orchestrator that runs autonomous agents with merge rights. If a compromised or buggy component can rewrite history, the log proves nothing. P0-2's fix (admin `acting_as` audit) depends on this log being trustworthy.

**Fix approach:**
1. Add SQLite triggers via migration: `CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;` and the same for `DELETE`.
2. Optional hardening: hash-chain column (`prev_hash`) so tampering via direct file edits is detectable. Defer unless threat model demands it — triggers are the 90% win.

**Tests to add:**
- `test_audit_update_rejected` / `test_audit_delete_rejected`: direct SQL UPDATE/DELETE on a row raises `IntegrityError`-class failure; row unchanged after.

---

## P2 — Quality, confidence, and positioning

### P2-1. Security and chaos test depth

**Why it matters:** 334 tests rate ~65% real confidence — strong on merge gate, concurrency, SSE; weak on adversarial inputs and failure injection. P0/P1 fixes above each add their own tests, but systemic gaps remain.

**Fix approach / tests to add:**
- Adversarial input suite: oversized payloads, malformed JSON, path-traversal attempts in worktree/task paths, HTML/SQL metacharacters in every string field that reaches DB or dashboard.
- Chaos suite: kill the process mid-merge and assert lock/worktree recovery on restart; corrupt/missing worktree dir on resume; SQLite busy/locked injection.
- One live-backend smoke test behind an env-gated marker (`@pytest.mark.live`) so the Claude-backend path isn't perpetually untested.

**Affected areas:** `tests/` (new files: `test_adversarial_inputs.py`, `test_chaos_recovery.py`), no production code unless tests find bugs.

### P2-2. Repo hygiene for a portfolio-grade project

**Why it matters:** Review verdict: staff-level engineering with a marketing gap. Missing CI means "334 green" is unverifiable by a visitor; missing LICENSE technically means all-rights-reserved.

**Fix approach:**
- GitHub Actions workflow: `uv sync` → `ruff check` → `mypy` → `pytest` on push/PR; add badge to README.
- Add `LICENSE` (MIT unless reason otherwise), `CHANGELOG.md` (seed from phase commits), `CONTRIBUTING.md` (short: uv, TDD, quality gates).
- Architecture diagram (one Mermaid block in README covering API → services → single-writer queue → SQLite/SSE) and a 30–60s demo GIF of the dashboard during a fleet run.

**Validation:** CI green on the PR itself is the test; README drift guard (`test_c4_readme_routes.py`) already protects the endpoint table.

---

## Suggested order of execution

1. P0-1 (XSS) — smallest diff, highest severity-to-effort ratio.
2. P0-3 (evidence SHA) — needs a migration; do while migration framework context is fresh.
3. P0-2 (identity binding) — touches auth + all tool handlers; do as one focused pass with the test trio.
4. P1-2 (budget atomicity) → P1-3 (audit triggers) → P1-1 (merge lock guard or SQLite lock).
5. P2 items as a follow-up PR.

All fixes TDD-first per repo convention: red test → minimal fix → full suite + ruff + mypy green before each commit.
