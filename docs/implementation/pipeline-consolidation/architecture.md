# Architecture — Pipeline Consolidation

Requirements: `./requirements.md`. No `design.md`/`design-system.md` — this is a backend/CLI feature with no new UI surface (NFR/non-goals: dashboard view is a follow-up, not in scope). One reviewer pass against this doc happens before implementation per skill workflow step 3.

## Dependency direction

```
Types (fleet/pipeline/models.py)
   ↓
Workflow definitions (fleet/pipeline/workflows.py)   — pure data, no I/O, ports hermes's FULL_SDLC
   ↓
PipelineService (fleet/pipeline/service.py)          — orchestrates DAG walk
   ↓
existing services it calls into (lower never imports higher):
   fleet/agents/service.py   (AgentService.create_agent)
   fleet/review/evidence.py  (EvidenceService.create_task / record_evidence / check_merge_gate)
   fleet/review/merge.py     (MergeService.execute_merge / check_gate)
   fleet/approvals/service.py (ApprovalService.request)
   fleet/events/service.py   (EventService.append)  — stage-transition events
   ↓
fleet/api/pipelines.py                               — new FastAPI routes, thin: parse request → call PipelineService → return record
```

`PipelineService` is the only new code that talks to multiple existing services — it is the seam. Nothing in `fleet/agents/`, `fleet/review/`, `fleet/approvals/`, or `fleet/events/` is modified; they are consumed as-is (NFR3).

## Sinks over pipes

`PipelineService.advance_run(run_id)` is the one entry point that mutates pipeline state. It is a sink: callers (the API route, or a future scheduler) call it and read its return value (`PipelineRun` with updated stage statuses) — they do not need to trace SSE events or DB triggers to know what happened. The SSE events it emits via `EventService.append` are a side-channel for live dashboards/observers only, explicitly named here per constitution principle 6, not a hidden control-flow path — `advance_run`'s own return value is authoritative.

## New module: `fleet/pipeline/`

```
fleet/pipeline/
  __init__.py
  models.py       # TaskSpec, Workflow, PipelineRun, PipelineStage (dataclasses + enums)
  workflows.py     # FULL_SDLC = Workflow(...) ported from hermes verbatim (FR1)
  planner.py       # build_plan(idea, workflow) -> list[PlannedStep]; powers `preview` (FR6), zero I/O
  service.py       # PipelineService: create_run, advance_run, get_run
  repository.py    # SQLite read/write for pipeline_run / pipeline_stage tables
```

### Data model (new migration `0007_pipeline_runs.sql`, matching existing migration style — plain SQL, numbered, one concern per file)

```sql
CREATE TABLE pipeline_run (
    id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    idea TEXT NOT NULL,
    scope TEXT NOT NULL,
    root_agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','blocked','done')),
    created_at TEXT NOT NULL
);

CREATE TABLE pipeline_stage (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_run(id),
    step_key TEXT NOT NULL,
    role TEXT NOT NULL,
    agent_id TEXT,                 -- NULL until spawned
    task_id TEXT,                  -- EvidenceService task id, NULL for non-worktree stages' merge gate
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','passed','failed')),
    UNIQUE (run_id, step_key)
);
```

`idempotency_key` format: `pipeline:{run_id}:{step_key}` (research.md decision). The `UNIQUE (run_id, step_key)` constraint plus a check in `PipelineService` ("if a row for this run_id+step_key is already `running`/`passed`, no-op") is what makes `advance_run` crash-safe-resumable — calling it twice for the same state does not double-spawn.

### `PipelineService` interface

```python
class PipelineService:
    def __init__(
        self, db: DatabaseManager, repo: PipelineRepository,
        agent_service: AgentService, evidence_service: EvidenceService,
        merge_service: MergeService, approval_service: ApprovalService,
        event_service: EventService,
    ) -> None: ...

    async def create_run(self, workflow: Workflow, idea: str, scope: str) -> PipelineRun: ...

    async def advance_run(self, run_id: str) -> PipelineRun:
        """Spawn any stage whose dependencies are all 'passed' and which is
        currently 'pending'. For 'impl'/'fix' stages, check the merge gate
        before marking 'passed'. On failure, call approval_service.request
        and set run.status = 'blocked'. Idempotent per stage via idempotency_key."""

    async def get_run(self, run_id: str) -> PipelineRun: ...
```

Constructor mirrors the existing pattern in `AgentService.__init__` (explicit service dependencies, no service locator) — consistent with how `fleet/agents/service.py` is already structured.

### New role-manifest entries (FR2)

Added to `fleet/manifests/default.yaml` under `roles:`, one block per hermes role. Tool sets assigned by stage shape, not copy-pasted from `coder`/`reviewer` wholesale:

- `pm`, `ux`, `architect`: read/write to scratch scope only, no merge/spawn tools — closest existing precedent is `observer` plus a `write_artifact`-class tool (new, minimal — write a single markdown file to the task's scratch dir). `Assumption`: fleet's toolserver doesn't yet have a scoped "write one file" tool; if true, this is a small addition in `fleet/toolserver/`, called out as its own implementation task rather than reusing `coder`'s broader file-write tool (least-privilege, matches manifest's fail-closed intent).
- `junior-dev`, `senior-dev` (review role): same tool shape as existing `coder` and `reviewer` roles respectively — reuse those `allowed_tools` lists rather than duplicating (DRY within the manifest).
- `junior-qa`, `senior-qa`: same shape as `reviewer` (validation/evidence tools), no merge-execution tool (only `senior-qa`'s gate decision should be able to clear `sqa`, mirroring evidence.py's non-owner-reviewer rule).
- `release`: `observer`-level read tools plus the single `write_artifact` tool for the handoff README.

### API surface (FR3, FR6)

`fleet/api/pipelines.py`, registered in `fleet/main.py` alongside the other routers (same `include_router` pattern already used there):

- `POST /api/pipelines` — body `{workflow: str, idea: str, scope: str}` → calls `create_run`, then `advance_run` once (kicks off the initial fan-out stages with no dependencies, i.e. `pm`).
- `POST /api/pipelines/{run_id}/advance` — re-check and spawn any newly-unblocked stages (called after a stage's evidence is recorded, or by an approval-queue resolution webhook).
- `GET /api/pipelines/{run_id}` — current `PipelineRun` + all `PipelineStage` rows.
- `POST /api/pipelines/preview` — body `{workflow: str, idea: str}` → calls `planner.build_plan` only, zero spawns (FR6). This is the only route with no `AgentService` dependency at all, structurally guaranteeing FR6's "zero calls" requirement rather than relying on a runtime check.

## What does NOT change

- `fleet/agents/`, `fleet/review/`, `fleet/approvals/`, `fleet/events/`, `fleet/workspace/`, `fleet/policy/`, `fleet/dashboard/` — zero edits. `fleet/manifests/default.yaml` gets additive new role blocks only (existing 4 roles untouched). `fleet/main.py` gets one new `include_router` line.

## Risks / explicit trade-offs

- **Risk**: 8 new roles is a lot of new manifest surface to get fail-closed-correct on the first pass. Mitigation: FR2's acceptance criteria requires a denial test per role before any role is considered done — no role ships untested.
- **Trade-off**: `PipelineService` is a synchronous-feeling orchestrator (`advance_run` does the DAG-walk math in Python) rather than a queue/worker model. Chosen because fleet's existing `AgentService` is already async/await over SQLite WAL, not a message queue — matching the existing architecture (constitution principle 7: minimal diff, fit the engine) beats introducing a new execution model for one feature.
