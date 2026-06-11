# PR #1 Review — Required Fixes

Source: multi-agent review of `fleet/phase-1-scaffold` vs `main` (2026-06-10).
Reviewers: critical-pass + testing/maintainability/security/performance/api-contract/data-migration specialists + Claude adversarial + Codex (cross-model) + Red Team + verification agent.

Verdict: **do not merge as-is.** 16 critical / ~30 informational findings.
Verified claims: ruff clean ✅, mypy strict clean ✅ (52 files), 191 tests pass **only** under `-m "not live and not slow"` (bare `pytest` → 32 failed, 62 errors).

Severity legend: **P0** = block merge. **P1** = fix on this branch before calling MVP done. **P2** = next branch / design work. **P3** = cleanup.

---

## P0 — Block merge (security + claim integrity)

### P0-1. Dashboard fully unauthenticated, including approval decisions
- `fleet/dashboard/router.py:572` — `POST /dashboard/approvals/{approval_id}/decide` has no auth. Anyone who can reach the server can approve budget overruns and risky operations. Unauthenticated back door around the human-in-the-loop gate (`/api/approvals/{id}/decide` is token-protected; this parallel route isn't).
- All dashboard GET routes (`router.py:301+`) leak agent rosters, full event payloads, worktree paths, approval queue.
- `fleet/config.py:42` — `validate_for_startup()` blesses token + non-loopback bind as safe while this surface stays open (false security guarantee).
- **Fix:** `APIRouter(dependencies=[Depends(require_token)])` on the dashboard router (or at minimum on the decide route). Confirmed by 5 independent reviewers; Codex recommendation was "block merge" on this exact finding.

### P0-2. Role ACL spoofable via caller-supplied `agent_id`
- `fleet/api/tools.py:277` — policy role is looked up from the request-body `agent_id`. Single shared `FLEET_API_TOKEN`; `agent_id` is an LLM-supplied MCP tool argument. Worker calls `list_agents`, learns orchestrator id, claims it → gains `spawn_worker` / `execute_merge` / `stop_agent`.
- `fleet/api/tool_handlers.py:81-92` — `spawn_worker` also honors caller-supplied `role` (worker can spawn an orchestrator outright).
- **Fix (minimum on this branch):** validate `inp.role` against an allowlist the caller's own role permits; document the identity-binding gap in ADR. **Real fix (P2-1):** per-agent token bound server-side to agent identity.

### P0-3. `GET /api/approvals` missing auth
- `fleet/api/approvals.py:60` — only `/api` route without `require_token`. Leaks pending operations, rationale, risk, requester ids.
- **Fix:** add `_auth: Annotated[None, Depends(require_token)]`. One line.

### P0-4. Bare `pytest` run is broken (claim integrity)
- Plain `uv run pytest -q` → `32 failed, 109 passed, 62 errors` (playwright/pytest-asyncio plugin clash when `slow` tests are collected). "191 tests passing" only true with `-m "not live and not slow"`.
- **Fix:** `addopts = "-m 'not live and not slow'"` in `[tool.pytest.ini_options]`, or fix the plugin clash. PR claim must match what a fresh clone experiences.

### P0-5. Suspicious unused dependency `httpx2`
- `pyproject.toml` — `httpx2>=2.3.0` installed, never imported anywhere in `fleet/` or `tests/`. Sits next to real `httpx`. Looks hallucinated (slopsquatting-style supply-chain risk).
- **Fix:** remove, `uv lock`, audit what it pulled in.

### P0-6. 73 committed `.pyc` files, no `.gitignore`
- `fleet/**/__pycache__/*.pyc` + test bytecode tracked (both cpython-312 and -313 variants). Can shadow renamed modules, bloats diff.
- **Fix:** add `.gitignore` (`__pycache__/`, `*.pyc`, `.pytest_cache/`, `.venv/`, `*.db`), then `git rm -r --cached` all `__pycache__` dirs.

---

## P1 — Fix on this branch (built-but-never-wired + merge gate integrity)

### Built-but-never-wired class
*(One end-to-end boot-and-smoke test through `fleet/main.py` would have caught every item below — add it as part of this section. Unit tests wire services manually; `main.py` is the wiring nobody tested.)*

- **P1-1. `restore_sessions()` never called** — `fleet/main.py` lifespan never invokes it (`fleet/agents/service.py:308`). Crash recovery is dead code: after restart, every agent freezes, inbox never drains. Fix: call it in lifespan with real backends + startup test.
- **P1-2. Workspaces router never mounted** — `fleet/api/workspaces.py` defines 10 endpoints; `fleet/main.py:122` never includes it, setters never called. All documented workspace endpoints 404 (would 500 if mounted). Also the only router with zero HTTP-level tests. Fix: mount + wire in lifespan + add `tests/test_workspace_api.py`; or delete the router.
- **P1-3. ClaudeBackend unreachable** — every spawn path hardcodes `MockBackend` (`fleet/api/tool_handlers.py:85`, `fleet/api/agents.py:51` only supports `"mock"`). All spawned agents are inert. Fix: backend factory mapping `backend_type` → backend; make `backend_type` a `Literal["mock","claude"]` so bad values 422 instead of 500.
- **P1-4. Spawn rate-limit counter always 0** — `fleet/api/tool_handlers.py:68` counts `state_change` events with `payload.action == 'spawn'`; no emitter ever sets that field. The "spawn rate enforcement" from Task 4.2 enforces against a phantom. Fix: emit `action: 'spawn'` in the spawn event payload + test that the counter increments.
- **P1-5. `task_description` dropped** — `fleet/api/tool_handlers.py:81` ignores it; spawned worker never receives its task. Fix: pass into `create_agent` and/or enqueue as first inbox message.
- **P1-6. `agents.worktree_id` never written** — only SELECTed; `worker_wip` always returns "no worktree" (`tool_handlers.py:181`). Fix: update after `create_worktree`.
- **P1-7. MCP schema drift** — `fleet/toolserver/main.py:63` `spawn_worker` missing `task_id` param (workers spawned via MCP can never get a worktree); `execute_merge` in `_TOOL_REGISTRY` and the orchestrator manifest but has no `@mcp.tool`. Fix: add both (or remove from manifest if human-only).
- **P1-8. `check_secret_path` dead code** — `fleet/policy/service.py:55` never called from production paths; `Settings.secret_patterns` unused. Manifest advertises protection that doesn't exist. Fix: wire into path-bearing tool dispatch, or delete + document.

### Merge gate integrity
- **P1-9. Merge lands on whatever HEAD points at** — `fleet/review/merge.py:321` runs `git merge --squash` + `git commit` without verifying HEAD == `base_branch` or that the main repo is clean. Fix: under the lock, assert `git symbolic-ref --short HEAD == base_branch` and clean porcelain on `repo_path`; abort otherwise.
- **P1-10. Cleanliness gate fails open** — `merge.py:386` `_git_porcelain` returns `''` on `GitError` (missing worktree passes as clean). Fix: treat git failure as gate failure (fail closed). Also reject worktrees with `status != 'active'` (re-merge of merged worktree possible).
- **P1-11. No `merge --abort`/reset on failure** — real squash can conflict where the old 3-arg `merge-tree` simulation didn't; repo left poisoned with conflict markers, and P1-10 means the next merge won't notice. Fix: on any merge-step failure, `git merge --abort`/`git reset --hard` and verify clean post-state.
- **P1-12. Lock keyed by scope, not repo** — `fleet/review/lock.py:34`; two scopes sharing one repository can mutate the same working tree concurrently. Fix: key by repo id/path (or scope+repo). Note: lock is also in-memory only — single-process deployment must stay documented invariant until a DB lock exists.
- **P1-13. Evidence joined by branch name; `task_id` never persisted** — `fleet/workspace/worktree_service.py:227` INSERT omits `task_id` (column exists, dashboard reads it); `merge.py:236` joins `tasks ON t.branch = w.branch`. Stale/attacker-created task with the same branch + passing evidence satisfies the gate for the wrong worktree. Fix: persist `task_id`, join on it.
- **P1-14. Stale `fail` rows block gate forever** — `fleet/review/evidence.py:211` fails on ANY historical `fail` row; a failed-then-passed check can never unblock. Fix: evaluate latest row per `check_name` only.

### ClaudeBackend correctness (blocks Phase 5 being "real")
- **P1-15. Sync Anthropic client blocks the event loop** — `fleet/agents/backends/claude.py:221` (streaming) and `:414` (`summarize`). One streaming turn freezes the entire fleet, SSE, dashboard, DB-writer future resolution. Fix: `AsyncAnthropic` + `async with` / `async for`; create the client once in `__init__` (currently rebuilt per turn, `:199`).
- **P1-16. Tool results silently dropped** — `:175` `pending_tool_results` cleared by `send()` but never flushed into `state.messages`; next API call after a `tool_use` turn lacks required `tool_result` blocks → Anthropic 400. Real backend breaks on first tool call. Fix: prepend pending results as a user-content block before clearing.
- **P1-17. Compaction is a no-op for the API context** — `:195` `state.messages` grows unbounded and full history is re-sent every turn; `AgentSession._compact` only trims its own `_conversation_history`. Fix: on compaction, reset backend history to the summary.

### Concurrency races
- **P1-18. Approvals `decide()` check-then-set** — `fleet/approvals/service.py:153` reads status, then UPDATEs without `WHERE status='pending'`; concurrent API + dashboard decisions both succeed, second silently overwrites. Fix: atomic `UPDATE ... WHERE id=:id AND status='pending'`, raise on rowcount==0.
- **P1-19. Worktree overlap TOCTOU** — `fleet/workspace/worktree_service.py:196` read-then-insert with awaits between; concurrent spawns create overlapping worktrees (violates ADR-006 ownership isolation). Fix: asyncio.Lock per repo, or re-verify inside the single-writer transaction.
- **P1-20. Spawn-cap TOCTOU** — `fleet/api/tool_handlers.py:56` read-then-act across multiple awaits; concurrent spawns exceed `max_live_workers`. Fix: serialize per scope or enforce in SQL.
- **P1-21. Archive/status race** — `fleet/agents/service.py:243` sets `archived` then cancels session; queued `_set_status` can resurrect the agent. Fix: cancel first, or guard writes with `WHERE status != 'archived'`.
- **P1-22. No graceful shutdown** — lifespan teardown only closes the DB manager; live session tasks never cancelled/drained, then write into a closed queue. Fix: `stop_all()` (cancel + await sessions) before `manager.close()`.
- **P1-23. Silent session-task death** — `fleet/agents/session.py:172` `backend.start()` outside try; `service.py:346` `create_task` with no done-callback. Backend failure → agent looks alive in DB, is dead. Fix: wrap + `_set_status('failed')` + done-callback that logs.

### Data integrity / ops
- **P1-24. WAL backup loses data** — `fleet/cli.py:202` `shutil.copy2` on live db misses `-wal` contents. Fix: `sqlite3` backup API or `VACUUM INTO`; optionally `PRAGMA quick_check` on the snapshot.
- **P1-25. Restore procedure corrupts** — `docs/ops/restore.md:28` copies `fleet.db` over live DB; stale `-wal`/`-shm` get replayed against the restored file. Fix: document deleting `-wal`/`-shm` + `PRAGMA integrity_check` before restart.
- **P1-26. Dashboard SSE broken in every config** — `fleet/static/app.js:120` `EventSource` cannot send the Authorization header `GET /api/events/stream` requires; empty-token config 401s everything too (`fleet/api/auth.py:46` rejects when no token configured, contradicting `validate_for_startup` loopback allowance — API unusable in the documented local dev config). Fix: decide one contract — short-lived cookie/query token for SSE, or loopback exemption — and align auth, config validation, and toolserver docs.

---

## P2 — Next branch (design work)

- **P2-1. Per-agent identity binding** — per-agent token (or server-injected identity) mapped to `agent_id`; policy uses authenticated identity, never a body field. Kills P0-2 properly. Biggest security win available.
- **P2-2. Evidence trust model** — gate currently passes on ≥1 self-attested row; worker can record its own `pytest=pass` (`tool_handlers.py:223-262`). Restrict who may record evidence; require reviewer verdict row.
- **P2-3. Prompt-injection hardening** — `fleet/agents/promptbuild.py:222` concatenates agent-written memory/team-state into the system prompt with spoofable `---` separators; one agent can inject instructions into another. Fence untrusted layers with nonce tags; strip separator sequences.
- **P2-4. Real migration framework** — `fleet/db.py:78` hardcodes `0001_init.sql`; a future `0002_*.sql` silently never runs; naive `split(';')` breaks on triggers/literals. Iterate `migrations/*.sql` sorted, track in `schema_migrations`, use `executescript`.
- **P2-5. Async hygiene sweep** — blocking sync git in async paths (`worktree_service.py:190,215,331,405,426`; `workspace/service.py:133`; ConflictChecker in `tool_handlers.py:212`) — wrap in `asyncio.to_thread` like `merge.py` already does. Sync DB reads on the loop (`fleet/db.py:170` and all callers) — add async read API mirroring the write path.
- **P2-6. SSE robustness** — catch-up capped at default `limit=200` with silent gap (`fleet/api/events.py:132`): page until exhausted. `QueueFull` drops silently (`fleet/events/sse.py:36`): close subscription so client resyncs via `Last-Event-ID`. `app.js:30` sends `after_id` param the server never reads: align client with `Last-Event-ID`. Document or prevent subscribe/catch-up duplicate delivery.
- **P2-7. Budget enforcement gaps** — denial doesn't stop the next turn (`session.py:428` — next inbox message runs a full paid turn before re-pausing); cost only checked post-turn; no per-scope aggregate cap.
- **P2-8. Schema fixes** — `validation_evidence.task_id` INTEGER vs `tasks.id` TEXT (`0001_init.sql:79`): change to TEXT. Add `inbox(to_agent_id, status)` and `events(agent_id, id)` indexes (hot-path scans). Decide FK policy for `inbox.to_agent_id`, `events.agent_id`, etc. (document intentional omissions).
- **P2-9. One timestamp format** — three coexist: `+00:00` isoformat (most services), `Z`-suffix (events/memory), SQL `strftime` DEFAULT on `validation_evidence.ts`. CLI doctor compares `Z`-strings lexicographically against `+00:00` data. Shared `utcnow_iso()` helper, used everywhere; drop the SQL DEFAULT.
- **P2-10. Restored sessions lose role** — `service.py:308` rebuilds every restored agent as default `worker` (orchestrators demoted on restart). Persist/restore role + task_description. Also `wait_for_decision` restart path never does the "check DB immediately" its comment claims (`approvals/service.py:208`).
- **P2-11. Relay client leak** — new `FleetRelay`/`httpx.AsyncClient` per tool call, `aclose()` never called (`toolserver/main.py:49`, `relay.py:37,82`). Construct once, reuse.
- **P2-12. Worktree base-branch drift** — DB records `base_branch=default_branch` but worktree is created from current HEAD (`worktree_service.py:214`, `gitops.py:209`). Pass the base branch explicitly.

---

## P3 — Cleanup (quality of life)

- API consistency: one error envelope for `dispatch_tool` (RFC7807 vs plain string mix, `tools.py:328`); `201` + response models on review endpoints (`review.py:69`); clamp `/api/events?limit` (`Query(le=1000)`, `events.py:89`); `MergeInProgressError` → 409 not 500 via tool path (`tool_handlers.py:372`).
- README endpoint table drifts from real routes (5+ mismatches: `/message` vs `/messages`, nonexistent `/api/agents/{id}/events`, merge/review paths, missing required `scope` param). Regenerate from `/openapi.json`.
- Maintainability: typed `ToolServices` dataclass instead of string-keyed `Any` dict (`tools.py:75`); fold `set_tool_services`' hidden policy-reset ordering contract (`tools.py:86`); consolidate 11 copy-pasted DI setter pairs (consider `app.state` + `Depends`); `EventType` Literal in `fleet/models.py` (bare event-type strings ×20+); hoist dashboard's triplicated 14-column SELECT; shared porcelain parser (`gitops.py:151` vs `:291`); shared relative-age formatter (cli vs dashboard); name doctor thresholds.
- Tests: `tests/conftest.py` with shared `wait_for_status`/`wait_for_event` + no-auth fixture (7 files copy-paste it); auth negative tests (wrong token, non-Bearer scheme, fail-closed empty-token branch — security-critical invariant untested); replace fixed `time.sleep`/`asyncio.sleep` waits with condition polling (dashboard smoke, lifecycle interrupt, compaction, merge-lock contention tests); use `monkeypatch.setenv` instead of mutating/deleting `os.environ['FLEET_API_TOKEN']`.
- Idle hibernate re-emits `waiting` event forever per idle agent (`session.py:187`) — only emit on transition.
- Event scrubbing is top-level-keys-only (`tools.py:134`) — recursive scrub + honor `Settings.secret_patterns`.
- `spawn_worker` swallows worktree-creation failure as success (`tool_handlers.py:97-115`) — return explicit degraded status.
- Validation report wording: AC-032 says agent goes "terminal"; actual (correct) behavior is resumable `paused_budget`.
- Fix deprecated `tool.uv.dev-dependencies` warning.

---

## Verified-good (keep doing)

- TDD discipline real: 0.85 test-to-code ratio, negative paths, red→green commits, dedicated quality-fix commits per task.
- `hmac.compare_digest` auth, bounded SSE queues, fail-closed policy 503, startup config validation.
- ADRs, runbooks, handoff docs, validation report — unusual ops maturity for an MVP.
- mypy strict from day one.

## Single highest-leverage addition

One **boot-and-smoke E2E test**: start the real app via `fleet/main.py` lifespan, hit one real flow over HTTP (register repo → spawn worker → tool call → event stream → approval → merge gate). Catches the entire built-but-never-wired class (P1-1…P1-8) and the auth/config contradictions (P0-1, P1-26) in one test.
