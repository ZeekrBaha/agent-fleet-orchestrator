# Implementation Plan — Pipeline Consolidation

Architecture: `./architecture.md`. Every task is RED→GREEN→REFACTOR per constitution principle 1. `uv run pytest -q`, `ruff`, `mypy` must stay clean after each task (NFR1, NFR2) — run them at the end of every task, not just at the end of the plan.

## Task sequence

Tasks are ordered bottom-up on the dependency direction in architecture.md (types → workflows → service → API), so each task's tests can exercise real lower layers instead of mocks where possible.

### T1 — Pipeline data model
**Files**: `fleet/pipeline/__init__.py`, `fleet/pipeline/models.py`, `tests/pipeline/test_models.py`
**Steps**: Define `TaskSpec`, `Workflow`, `PipelineRun`, `PipelineStage` dataclasses + `StageStatus`/`RunStatus` enums. No I/O.
**Test first**: construct a `Workflow` with 2 tasks + 1 edge, assert fields round-trip; assert `StageStatus` enum has exactly `pending/running/passed/failed`.
**Acceptance (FR1 partial)**: model module has zero imports from `fleet/agents`, `fleet/review`, `fleet/api` (pure data layer, checked by import-only test).

### T2 — Port FULL_SDLC workflow
**Files**: `fleet/pipeline/workflows.py`, `tests/pipeline/test_workflows.py`
**Steps**: Port hermes's `FULL_SDLC` `Workflow`/`TaskSpec` definition verbatim (9 steps, edges from research.md).
**Test first**: assert `FULL_SDLC.edges == (("pm","ux"), ("pm","arch"), ("ux","impl"), ("arch","impl"), ("impl","review"), ("review","fix"), ("fix","jqa"), ("jqa","sqa"), ("sqa","handoff"))` — hard-coded regression guard against drift (FR1 acceptance).
**Acceptance**: edge tuple matches hermes exactly; step_key/role/profile/workspace fields match research.md table.

### T3 — Migration 0007 + PipelineRepository
**Files**: `fleet/migrations/0007_pipeline_runs.sql`, `fleet/pipeline/repository.py`, `tests/pipeline/test_repository.py`
**Steps**: Add `pipeline_run`/`pipeline_stage` tables per architecture.md schema. `PipelineRepository` wraps `DatabaseManager` (same pattern as `EventService`/`ApprovalService` constructors — `db: DatabaseManager` injected).
**Test first**: insert a run + 2 stages, assert `UNIQUE(run_id, step_key)` constraint rejects a duplicate insert (this is what makes `advance_run` idempotent later — test the DB guarantee in isolation first).
**Acceptance**: migration applies cleanly on top of `0006_audit_append_only.sql` in the existing migration runner; round-trip read/write for both tables.

### T4 — Planner (preview, zero I/O)
**Files**: `fleet/pipeline/planner.py`, `tests/pipeline/test_planner.py`
**Steps**: `build_plan(idea: str, workflow: Workflow) -> list[PlannedStep]` — pure function, ports hermes's `planner.py` logic (title templating, slug, idempotency_key formatting) minus any Kanban-specific fields.
**Test first**: `build_plan("Build X", FULL_SDLC)` produces 9 `PlannedStep`s with `idempotency_key == f"pipeline:{run_id}:{step_key}"` shape and titles matching the `{title}` template substitution hermes used.
**Acceptance (FR6 partial)**: function takes zero service dependencies — structurally cannot call `AgentService` (no parameter to do so).

### T5 — New role-manifest entries
**Files**: `fleet/manifests/default.yaml`, `tests/policy/test_pipeline_roles.py`
**Steps**: Add 8 role blocks per architecture.md tool-set mapping. If a `write_artifact` scoped tool doesn't exist yet, add it minimally in `fleet/toolserver/` first (sub-task T5a) with its own RED→GREEN cycle before wiring it into manifests.
**Test first**: for each of the 8 new roles, spawn a mock agent with that role and assert calling a tool NOT in its `allowed_tools` is denied (reuses existing fail-closed test pattern — find and follow the existing test for `coder`/`reviewer` denial as the template).
**Acceptance (FR2)**: 8/8 roles have a passing denial test; existing 4 roles' tests unchanged and still green.

### T6 — PipelineService.create_run + advance_run (happy path)
**Files**: `fleet/pipeline/service.py`, `tests/pipeline/test_service.py`
**Steps**: Implement per architecture.md interface. `advance_run` walks `Workflow.edges` to find stages whose deps are all `passed` and which are `pending`; spawns via `AgentService.create_agent` using `MockBackend`; for non-worktree stages marks `passed` once the agent's single turn completes (no merge gate); for worktree stages (`impl`/`fix`) leaves `running` until T7 wires the gate check.
**Test first**: `create_run(FULL_SDLC, "Build X", scope)` then `advance_run` — assert `pm` stage transitions to `running` (no other stage does yet, since everything else depends on `pm`).
**Acceptance (FR3 partial)**: full DAG order test — repeatedly call `advance_run` against `MockBackend` until all 9 stages reach a terminal status; assert `impl` never enters `running` before both `ux` and `arch` are `passed` (the fan-in case from research.md).

### T7 — Evidence + merge gate wired for impl/fix
**Files**: `fleet/pipeline/service.py` (extend), `tests/pipeline/test_service_merge_gate.py`
**Steps**: For `impl`/`fix` stages, `advance_run` calls `EvidenceService.create_task` on spawn and `check_merge_gate` before marking `passed`.
**Test first**: spawn `impl`, record one evidence entry with status `fail`, call `advance_run` again, assert stage stays `running`/`failed` and the DAG does not advance to `review` (FR4 acceptance criteria, verbatim from requirements.md).
**Acceptance (FR4)**: passing case (reviewer verdict recorded, gate passes) advances to `review`; failing case blocks per T8.

### T8 — Failure routes to approval queue
**Files**: `fleet/pipeline/service.py` (extend), `tests/pipeline/test_service_failure.py`
**Steps**: On gate failure or agent error, call `ApprovalService.request(...)` with run id/stage key/reason; set `pipeline_run.status = 'blocked'`.
**Test first**: trigger the T7 failing-gate case, assert an approval entry exists via `ApprovalService.list_pending` and `run.status == "blocked"` (not `"failed"`, per requirements.md FR5 exact wording).
**Acceptance (FR5)**: matches requirements.md FR5 acceptance criteria exactly.

### T9 — SSE events on stage transitions
**Files**: `fleet/pipeline/service.py` (extend), `tests/pipeline/test_service_events.py`
**Steps**: Every stage status transition calls `EventService.append` (sinks-over-pipes — this is the declared side-channel, architecture.md).
**Test first**: advance a run through 2 stage transitions, assert `EventService.query` returns 2 corresponding events with the run/stage ids in the payload.
**Acceptance**: event shape matches existing `EventService` event schema (reuse, don't invent a new one).

### T10 — API routes
**Files**: `fleet/api/pipelines.py`, `fleet/main.py` (one `include_router` line), `tests/api/test_pipelines_api.py`
**Steps**: Implement the 4 routes from architecture.md. `preview` route constructed with no `AgentService` dependency injected at all (structural FR6 guarantee).
**Test first**: `POST /api/pipelines/preview` with a body, assert 200 + 9-step table in response, then assert (via dependency-injection inspection or a spy on `AgentService.create_agent`) zero spawns occurred.
**Acceptance (FR6)**: matches requirements.md acceptance criteria exactly; full route set has request/response schema tests.

### T11 — Full end-to-end pipeline integration test
**Files**: `tests/pipeline/test_e2e_full_sdlc.py`
**Steps**: No new production code — this is the "verify in runtime" gate (constitution principle 5). Run `FULL_SDLC` start to finish against `MockBackend` through the real API routes (not service-layer calls directly), asserting final state is `pipeline_run.status == "done"` with all 9 stages `passed`, and that the worktree/merge artifacts for `impl`/`fix` are real (not mocked away).
**Acceptance**: this test is the thing constitution principle 9's independent refutation pass tries hardest to break.

### T12 — harness-engineering SOP fold-in
**Files**: `docs/reference/layered-domain-architecture.md`, `docs/reference/evaluator-rubric.md`, `docs/reference/clean-state-checklist.md`, `docs/reference/quality-document.md` (new `docs/reference/` folder)
**Steps**: Copy the 4 files from `harness-engineering/{sops,templates}/`, add a one-line provenance header to each (`> Source: harness-engineering, folded in <date> as role-manifest source-of-truth`).
**Acceptance (FR8)**: files exist; no code changes; not test-covered (docs-only task, noted as the one exception to "every task has a test" — acceptable per skill's own guidance that not every task can be tested, but flagged here rather than silently skipped).

### T13 — Donor repo archival
**Files**: none in this repo — operates on the two donor repos.
**Steps**: Tag `archived-into-fleet-orchestrator` on `hermes-ai-software-team-pipeline` and `harness-engineering`; add README banner to each pointing here.
**Acceptance (FR9)**: manual, checked off in `progress.md` only after T1–T12 are green and the independent refutation pass (validation-plan.md) completes. This is explicitly last — never archive donors before the port is proven.

## Role review notes (PM / Dev / Junior Dev / Tester / Reviewer / Team Lead)

Reuses the existing role prompts already in this repo at `docs/prompts/{team-lead,developer,junior-developer,tester,reviewer}.md` — no new prompt set needed; this feature is built inside the same repo those prompts already govern. `agent-assignments.md` (next doc) maps T1–T13 to which of those existing prompt-roles owns each task.

## Risks / rollback

- If T5's 8-role manifest surface proves too large to get fail-closed-correct quickly, descope to the 2 roles `impl`/`fix` actually need (`junior-dev`, `senior-dev`) for a v1 that proves the engine, and stub `pm/ux/arch/jqa/sqa/release` as `observer`-equivalent placeholders — flag this as a scope-reduction decision in `progress.md` if taken, don't silently narrow FR2.
- Rollback unit: each task is its own commit; reverting T6 alone (say, the DAG walk has a bug found late) doesn't require reverting T1–T5.
