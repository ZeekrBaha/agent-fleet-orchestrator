# Requirements — Pipeline Consolidation

Research: `./research.md`. Constitution: `./constitution.md`.

## Decisions on research.md's open questions

Per Auto Mode (proceed on reasonable calls, flag for override), each open question gets a stated `Decision` + `Assumption` rather than blocking. User can redirect any of these.

- **Worktree scope.** `Decision`: mirror hermes exactly — only `impl` and `fix` stages get `workspace="worktree"` + a real branch + evidence-gated merge. `pm`, `ux`, `arch`, `review`, `jqa`, `sqa`, `handoff` run in fleet's existing `scratch` scope (no worktree, no merge gate), but still call `EvidenceService.record_evidence` once with their output artifact path so the pipeline run has an auditable trail end-to-end, not just for the two coding stages.
- **Stage failure handling.** `Decision`: on a stage's evidence gate failing (for `impl`/`fix`) or an agent erroring out (any stage), halt the DAG and push to fleet's existing approval queue (`approvals/`) with the failure reason. No auto-retry in v1 — retry is a stated non-goal below.
- **Idempotency.** `Decision`: keep `idempotency_key` as a column on the new `pipeline_stage` row (format `pipeline:{run_id}:{step_key}`, ported verbatim from hermes's scheme). Used to make `start stage` a no-op if called twice for the same run+step (crash-safe resume), not for Kanban dedup since Kanban is gone.

## Functional requirements

**FR1 — Workflow definition ported**
Fleet gets a `Workflow`/`TaskSpec` data model (dataclasses, no I/O) equivalent to hermes's, including the `FULL_SDLC` DAG (9 steps, fan-out at `pm`, fan-in at `impl`). Acceptance: a unit test asserts the ported `FULL_SDLC.edges` matches hermes's edge tuple exactly (regression guard against silent drift during port).

**FR2 — New role-manifest entries**
8 new roles added to fleet's role manifest (`pm`, `ux`, `architect`, `junior-dev`, `senior-dev`, `junior-qa`, `senior-qa`, `release`), each with an explicit `allowed_tools` list (fail-closed — no role inherits a broader tool set than it needs). Acceptance: a test spawns an agent with each new role and asserts policy denies a tool not in that role's `allowed_tools` (reuses fleet's existing fail-closed test pattern).

**FR3 — Pipeline run orchestration**
A new service walks the DAG: for each stage whose dependencies (incoming edges) have completed, call `POST /api/agents` with `role`, `parent_id` set to the pipeline run's root agent, and `scope` per the FR1 decision above. Acceptance: an integration test runs `FULL_SDLC` against fleet's `MockBackend` end-to-end and asserts all 9 stages reach a terminal status in dependency order (e.g. `impl` never starts before both `ux` and `arch` are done).

**FR4 — Evidence + merge gate wired for coding stages**
`impl` and `fix` stages create a worktree task via `EvidenceService.create_task`, and the DAG only advances past them once `check_merge_gate` passes. Acceptance: a test fails the gate (no reviewer verdict recorded) and asserts the DAG halts rather than advancing to the next stage.

**FR5 — Failure routes to approval queue**
A stage failure (gate fail or agent error) creates an approval-queue entry with the run id, stage key, and failure reason; the DAG does not proceed past the failed stage until approved. Acceptance: test triggers a stage failure, asserts an approval entry exists and the run's status is `blocked`, not `failed` or silently `running`.

**FR6 — CLI/API entry point preserved**
A `preview` capability equivalent to hermes's `team-pipeline preview --idea "..."` is preserved (prints the same step table: step_key, title, assignee, workspace, idempotency_key) and makes zero network/spawn calls, matching hermes's original "AC3.1" guarantee. Acceptance: test asserts `preview` produces output without any `AgentService.create_agent` call being invoked (mock/spy assertion).

**FR7 — Hermes Kanban dependency fully removed**
No code path in the ported feature shells out to a `hermes` binary or imports `kanban_client.py`. Acceptance: `grep -ri "hermes" fleet/pipeline/` (new module) returns nothing except this requirements doc's own comments/docstrings referencing the migration history.

**FR8 — harness-engineering SOPs folded in**
`layered-domain-architecture.md`, `evaluator-rubric.md`, `clean-state-checklist.md`, `quality-document.md` copied into fleet's `docs/reference/` (new folder) with a top-of-file note crediting the harness-engineering origin and stating they are now the source-of-truth for this repo's role-manifest conventions. Acceptance: files exist at the new path; original harness-engineering repo is unaffected (copy, not move, until archival step).

**FR9 — Donor repo archival**
After FR1–FR8 are validated (validation-plan.md), `hermes-ai-software-team-pipeline` and `harness-engineering` each get: a git tag `archived-into-fleet-orchestrator`, and a README banner pointing to the new location. Acceptance: manual step, checked off in `progress.md`, not unit-testable.

## Non-functional requirements

- **NFR1**: All 371+ existing fleet-orchestrator tests stay green throughout. No existing test is modified to accommodate new code unless it tests behavior this feature intentionally changes (none should).
- **NFR2**: `ruff` and `mypy` stay clean on the full repo after every task.
- **NFR3**: New module is additive — `fleet/pipeline/` is new; existing `fleet/agents/`, `fleet/review/`, `fleet/api/` files get at most new route registrations, not behavioral edits, unless a task explicitly requires it.

## Non-goals (v1)

- Auto-retry of a failed stage (manual approval-queue resolution only).
- Parallel execution of multiple pipeline runs sharing budget pools (single-run-at-a-time is acceptable for v1; fleet's existing per-agent budget controls apply per spawned agent regardless).
- A new dashboard view for pipeline runs (out of scope — SSE events are emitted per constitution principle 6, but a dedicated UI panel is a follow-up, not blocking this initiative).
- Porting hermes's Jinja2 templates (`pm_spec.md.j2` etc.) verbatim — fleet already has `fleet/prompts/roles/*.md`; new role prompts follow that existing convention instead of introducing a second templating system. `Decision`, not `Assumption` — avoids the "two ways to do the same thing" anti-pattern.

## Traceability

| Requirement | Source |
|---|---|
| FR1 | research.md Repo 2 facts (Workflow/TaskSpec/edges) |
| FR2 | research.md Repo 1 facts (manifest schema) + Repo 2 facts (8 roles) |
| FR3 | research.md "no pipeline concept exists" gap |
| FR4 | research.md Repo 1 evidence/merge gate facts |
| FR5 | research.md open question, resolved above |
| FR6 | research.md Repo 2 `preview` AC3.1 fact |
| FR7 | research.md Repo 2 Kanban adapter facts |
| FR8 | research.md Repo 3 file inventory |
| FR9 | constitution.md principle 8 |
