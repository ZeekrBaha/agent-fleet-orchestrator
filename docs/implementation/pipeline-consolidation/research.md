# Research — Pipeline Consolidation

Constitution: `./constitution.md`. Labels: `Repository fact`, `Evidence`, `Assumption`.

## Goal

Merge `hermes-ai-software-team-pipeline`'s role-graph workflow and `harness-engineering`'s SOPs into `agent-fleet-orchestrator`, replacing the external Hermes Kanban dependency with fleet's own spawn/evidence/merge APIs. Archive the two donor repos once ported and validated.

## Repo 1 — agent-fleet-orchestrator (target/core)

`Repository fact`: Module layout under `fleet/`: `agents/` (lifecycle, backends), `api/` (FastAPI routes), `approvals/`, `dashboard/` (htmx+SSE), `events/` (SSE), `manifests/` (YAML policy), `memory/` (compaction), `migrations/` (SQLite schema), `policy/` (manifest validation), `prompts/` (agent system prompts), `review/` (merge gate, evidence, conflict check), `toolserver/` (MCP), `workspace/` (git worktree ops). 57 test modules in `tests/`.

`Repository fact`: Test command is `uv run pytest -q` (371 tests, `-m 'not live and not slow'` filters by default). `ruff` and `mypy` both clean.

### Role-manifest schema (`fleet/manifests/default.yaml`)

```yaml
version: "1"

roles:
  <role_name>:
    allowed_tools:
      - <tool_name>          # MCP tool names: spawn_worker, send_message, execute_merge, ...
    spawn_rate:
      max_live_workers: <int>
      max_spawns_per_minute: <int>

secret_paths:
  - "**/.env"
  - "**/*.pem"
  - "**/.aws/**"

shell_rules:
  timeout_s: <int>
  env_allowlist:
    - HOME
    - PATH
    - LANG
    - USER
```

`Repository fact`: existing roles are `orchestrator` (10 max live workers, 5 spawns/min, tools: spawn_worker, send_message, execute_merge, request_approval), `coder` (8 tools incl. worker_wip, check_conflict, record_validation), `reviewer` (6 tools, validation/evidence focus), `observer` (read-only: list_agents, get_agent_logs). Fail-closed: anything not in `allowed_tools` is denied.

### Spawn API — `POST /api/agents`

`Repository fact`, file `fleet/api/agents.py`:

```python
class AgentCreate(BaseModel):
    scope: str
    name: str
    role: str
    model: str
    backend_type: Literal["mock", "claude"] = "mock"
    parent_id: str | None = None
    repository_id: str | None = None
    budget_soft_usd: float | None = None
    budget_hard_usd: float | None = None

@router.post("", response_model=AgentRecord, status_code=201)
async def create_agent(
    body: AgentCreate,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> AgentRecord:
    backend = _make_backend(body.backend_type)
    return await service.create_agent(
        scope=body.scope, name=body.name, role=body.role, backend=backend,
        model=body.model, parent_id=body.parent_id, repository_id=body.repository_id,
        budget_soft_usd=body.budget_soft_usd, budget_hard_usd=body.budget_hard_usd,
        backend_name=body.backend_type,
    )
```

`role` must match a key in the active role manifest, or the policy layer rejects it fail-closed (per constitution principle 9, confirm this rejection path has a test before relying on it).

### Evidence-gated merge — `fleet/review/`

`Repository fact`: `EvidenceService` — `create_task(scope, title, description, owner_agent_id, branch, acceptance_criteria)`, `record_evidence(task_id, check_name, status, output, recorded_by, recorded_by_role, commit_sha)`, `check_merge_gate(task_id, branch_sha) -> (bool, reason)`. Gate logic (evidence.py:193-282): requires ≥1 evidence record, blocks on any "fail" status, requires a non-owner reviewer-role verdict (no self-attestation), blocks on stale commit SHA.

`MergeService` — `execute_merge(worktree_id, agent_id, scope) -> MergeResult(commit_sha, branch, task_id)`, `check_gate(worktree_id) -> GateStatus`.

### Pipeline/workflow concept

`Repository fact`: **none exists.** Agent lifecycle is `spawn → turn → compact → hibernate → merge`, each agent autonomous. No persistent task-graph object, no stage sequencing, no concept of "agent B starts after agent A's evidence gate passes." This confirms the consolidation is net-new capability for fleet-orchestrator, not a relabeling exercise.

## Repo 2 — hermes-ai-software-team-pipeline (donor)

`Repository fact`: Role graph lives in `src/team_pipeline/workflow.py` as a `Workflow` dataclass (`name`, `tasks: tuple[TaskSpec, ...]`, `edges: tuple[tuple[str, str], ...]`). The `FULL_SDLC` workflow:

```python
FULL_SDLC = Workflow(
    name="full-sdlc",
    tasks=(
        TaskSpec(step_key="pm",     profile="pm-agent",          role="pm",         template="pm_spec.md.j2", ...),
        TaskSpec(step_key="ux",     profile="ux-designer-agent",  role="ux",         template="ux_design.md.j2", ...),
        TaskSpec(step_key="arch",   profile="architect-agent",    role="architect",  template="architecture.md.j2", ...),
        TaskSpec(step_key="impl",   profile="junior-dev-agent",   role="junior-dev", workspace="worktree", branch="wt/{slug}-impl", ...),
        TaskSpec(step_key="review", profile="senior-dev-reviewer",role="senior-dev", ...),
        TaskSpec(step_key="fix",    profile="junior-dev-agent",   role="junior-dev", workspace="worktree", ...),
        TaskSpec(step_key="jqa",    profile="junior-qa-agent",    role="junior-qa", ...),
        TaskSpec(step_key="sqa",    profile="senior-qa-agent",    role="senior-qa", ...),
        TaskSpec(step_key="handoff",profile="release-agent",      role="release", ...),
    ),
    edges=(
        ("pm","ux"), ("pm","arch"), ("ux","impl"), ("arch","impl"),
        ("impl","review"), ("review","fix"), ("fix","jqa"), ("jqa","sqa"), ("sqa","handoff"),
    ),
)
```

`Repository fact`: this is a **DAG**, not a strict line — `impl` depends on both `ux` and `arch` completing (fan-in); `pm` fans out to `ux` and `arch` in parallel.

`Repository fact`: Hermes Kanban integration is a subprocess adapter (`src/team_pipeline/kanban_client.py`) shelling out to a `hermes` CLI binary (`kanban --board ... create ... --assignee ... --workspace ... --idempotency-key ... --json`). No HTTP/SDK — pure subprocess + JSON stdout parsing. This is the entire surface to remove.

`Repository fact`: `team-pipeline preview --idea "..."` (cli.py) calls `load_workflow()` → `build_plan(idea_record, wf)` → prints a table. Explicitly makes **zero** Hermes calls — plan-building is already decoupled from the Kanban adapter. This is the part worth porting as-is.

`Repository fact`: test command `uv run pytest tests/ -v`.

## Repo 3 — harness-engineering (donor, docs-only)

`Repository fact`: 16 files total.
- `sops/` (4): `chrome-devtools-validation-loop.md`, `encode-knowledge-into-repo.md`, `layered-domain-architecture.md`, `observability-feedback-loop.md`.
- `templates/` (8): `AGENTS.md`, `CLAUDE.md`, `claude-progress.md`, `session-handoff.md`, `clean-state-checklist.md`, `quality-document.md`, `evaluator-rubric.md`, `index.md`.
- `reference/` (4): `coding-agent-startup-flow.md`, `initializer-agent-playbook.md`, `method-map.md`, `prompt-calibration.md`.

`Assumption`: not all 16 are relevant to fleet-orchestrator's role-manifest docs. Only the ones describing role/policy/quality conventions are in scope (`layered-domain-architecture.md`, `evaluator-rubric.md`, `clean-state-checklist.md`, `quality-document.md` are the clearest fits). The rest (chrome-devtools loop, prompt-calibration, startup-flow) are general harness doctrine, not role-manifest source material — out of scope for this port unless requirements.md says otherwise.

## Consolidation shape (factual conclusion, not yet a design decision)

1. Hermes's `Workflow`/`TaskSpec`/edges dataclasses port near-verbatim into a new `fleet/pipeline/` module — they have no Hermes Kanban coupling.
2. The 8 hermes roles (`pm`, `ux`, `architect`, `junior-dev`, `senior-dev`, `junior-qa`, `senior-qa`, `release`) do not exist in fleet's current role manifest (`orchestrator`, `coder`, `reviewer`, `observer`). New manifest entries are required — this is a design decision, not a fact, deferred to `design.md`/`architecture.md`.
3. The Kanban subprocess client is replaced by calls to fleet's own `POST /api/agents` (spawn) + `EvidenceService`/`MergeService` (stage completion gating) — DAG edges become "spawn next stage's agent only after prior stage's evidence-gated merge passes."
4. harness-engineering's relevant SOPs become reference docs under fleet's `docs/`, not new code.

## Open questions for requirements.md

- Does a stage need its own git worktree + branch (hermes sets `workspace="worktree"` only for `impl`/`fix`), or do non-coding stages (pm/ux/arch/review/qa/release) run in `scratch` scope with no merge gate at all?
- What happens on a stage failure — does the DAG halt, retry, or escalate to approval queue (fleet already has one)?
- Is `idempotency_key` (hermes concept) still needed without the Kanban backend, e.g. for safe pipeline-run resume after a crash?
