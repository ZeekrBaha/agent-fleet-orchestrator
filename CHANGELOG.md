# Changelog

All notable changes to agent-fleet-orchestrator are documented here.

## [Unreleased] — Phase 1 P2 (current branch)

### Added
- CI workflow (`.github/workflows/ci.yml`): uv sync → ruff → mypy → pytest on every push/PR
- Adversarial input test suite (`tests/test_adversarial_inputs.py`): field length limits, malformed JSON, enum constraints, metacharacter injection (HTML/SQL/path traversal/unicode)
- Chaos and recovery test suite (`tests/test_chaos_recovery.py`): missing worktree path, non-git directory, concurrent evidence writes, gate on non-existent task
- MIT `LICENSE`
- This `CHANGELOG.md`

### Fixed
- `_handle_record_validation` now emits a `WARNING` log when SHA resolution fails (missing dir or non-git path), rather than failing silently
- SHA resolution degrades safely for both `GitError` and `OSError` (missing directory)

---

## Phase 1 — Security and Correctness Fixes

### P0 blockers

- **P0-1** (`a8e55b3`): Escape exception messages in dashboard `HTMLResponse` (XSS)
- **P0-2** (`13234aa`): Inject `_authenticated_agent_id` into handler svcs; use token-authenticated identity for attribution instead of body-supplied `agent_id`
- **P0-3** (`1c3b9b0`, `1a4627f`): Bind evidence to commit SHA; reject stale (NULL-SHA) evidence at merge gate; stamp server-resolved worktree HEAD SHA from `record_validation` tool path; fail closed on `GitError` at both gate call sites

### P1 fixes

- **P1-1** (`b8e1c0c`): Refuse startup when `WEB_CONCURRENCY > 1` (single-process SQLite invariant)
- **P1-2** (`0d3c934`): Atomic budget check+reserve to prevent TOCTOU overshoot
- **P1-3** (`067f702`): SQLite triggers enforce append-only on events audit log

### Phase fixes (A/B/C/T series)

- **A1** (`d103d59`): Per-agent identity binding — SHA-256 token auth on tool dispatch
- **A2** (`e3fef88`): Evidence trust model — reviewer verdict required to open merge gate
- **A3** (`50b3dea`): Prompt-injection hardening — nonce-fenced untrusted layers
- **B1** (`db383ad`): Real migration framework with `schema_migrations` tracking
- **B2** (`008c626`): Schema and timestamp unification
- **B3** (`b654b2d`): Wrap all sync git calls in `asyncio.to_thread`
- **B4** (`e49592f`): SSE robustness — pagination, `QueueFull` close, `Last-Event-ID` client
- **B5** (`fd0c354`): Pre-turn budget gate + per-scope aggregate cap
- **B6** (`8729cbb`): Restore sessions with correct role; fix approval restart hang
- **B7** (`69b44da`): Relay singleton + worktree base-branch drift
- **C1** (`940e408`): API consistency — events `limit` 422, `MergeInProgressError` 409, `create_task` 201
- **C2** (`57c63e9`): Auth negative regression tests + hibernate transition-only emit
- **C3** (`312a33e`): `spawn_worker` returns explicit `worktree_status` instead of swallowing failure
- **C4/C5** (`b2bff88`): README endpoint table, `EventType` Literal, shared conftest, monkeypatch
- **T4** (`5963a99`): Fix backend factory + spawn wiring (P1-3 through P1-6)
- **T5** (`1a04ee3`): Fix `main.py` lifespan wiring (P1-1, P1-2, P1-22)
- **T6** (`b548ef9`): Wire `check_secret_path` + fix MCP toolserver schema drift (P1-7/P1-8)
- **T8** (`a6c1506`): Resolve five concurrency race conditions
- **T10** (`618d236`): Fix SSE auth — query-param token + loopback bypass
- **T11** (`630c310`): Correct `tmp_path` type annotation; remove spurious `SimpleNamespace` import

---

## Phase 1 Scaffold — Initial

- **`16ef5ee`**: Task 1.1 scaffold + config — initial project structure
