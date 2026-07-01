# Validation Plan — Pipeline Consolidation

Implementation plan: `./implementation-plan.md`.

## Commands (run after every task, not just at the end)

```bash
cd ~/Desktop/llm-ai-projects/agent-fleet-orchestrator
uv run pytest -q                 # full suite, must stay green incl. existing 371
uv run pytest -q tests/pipeline  # new module's tests, run on their own too
uv run ruff check .
uv run mypy fleet/
```

No new command needed — this feature reuses the existing repo's exact toolchain (NFR2). If `uv run pytest -q` filters `-m 'not live and not slow'` by default per the README, the new e2e test (T11) must NOT be marked `live` or `slow` unless it genuinely calls a real Claude backend — it runs against `MockBackend`, so it should run in the default fast suite.

## Per-FR verification

| Requirement | How verified |
|---|---|
| FR1 | T2's hard-coded edge-tuple test |
| FR2 | T5's 8 denial tests (one per new role) |
| FR3 | T6's DAG-order test (fan-in/fan-out ordering) |
| FR4 | T7's gate-fail-blocks-advance test |
| FR5 | T8's approval-entry + `status == "blocked"` test |
| FR6 | T4's zero-dependency planner + T10's spy-on-create_agent test |
| FR7 | `grep -ri "hermes" fleet/pipeline/` returns nothing (run as a CI-style check, not just a one-off) |
| FR8 | T12 — files exist at `docs/reference/`, manual check |
| FR9 | T13 — manual, checked off in progress.md last |

## Anti-slop / scope checks (non-UI variant)

No visual gate applies (no UI). The equivalent discipline here:
- Confirm no existing test in `tests/agents/`, `tests/review/`, `tests/approvals/`, `tests/events/` was modified — `git diff --stat` against those directories should be empty after the full initiative (NFR3 proof).
- Confirm `fleet/manifests/default.yaml`'s existing 4 roles are byte-identical except for new blocks appended (diff the YAML, not just "tests still pass").

## Independent refutation pass (constitution principle 9 — mandatory before declaring done)

After T1–T12 are green, dispatch a **fresh** subagent/session (not the one that wrote the code) with this brief: *"Try to prove the pipeline-consolidation port does NOT actually reproduce hermes's FULL_SDLC step graph, or does NOT run on fleet's own spawn/evidence/merge APIs without any Hermes Kanban dependency. Look specifically for: (a) the edge tuple silently drifting from research.md's quoted version, (b) any remaining subprocess call to a `hermes` binary anywhere in the new code path, (c) a stage that can reach `passed` without going through `AgentService.create_agent` or without `EvidenceService`/`MergeService` actually being invoked for impl/fix, (d) the fan-in case (`impl` waiting on both `ux` and `arch`) being silently satisfied by only one parent completing."*

Default to "not done" until this pass fails to find a break. Record the result (what was checked, what was found if anything, pass/fail) in `validation-report.md`.

## What "done" means

All of: full test suite green (371 existing + new pipeline tests), ruff/mypy clean, FR1–FR9 table above all checked, refutation pass recorded as failing-to-break (i.e. the port held up), `progress.md` shows T1–T13 complete, donor repos tagged and bannered (T13, last).
