# Constitution — Pipeline Consolidation

Governs every doc and every line of code produced for this initiative. If a later doc conflicts with this file, this file wins.

## Scope of this initiative

Merge the role-graph workflow from `hermes-ai-software-team-pipeline` (PM → UX → Architect → Junior Dev → Senior Dev Review → Fix → Junior QA → Senior QA → Release) into `agent-fleet-orchestrator` as a built-in pipeline template running on fleet's own spawn API — replacing the external Hermes Kanban dependency. Fold `harness-engineering`'s SOPs/templates into fleet's docs as the source-of-truth for role manifests. Archive the two donor repos once ported.

This is **consolidation, not a rewrite**. The fleet-orchestrator engine (371 tests green, FastAPI/asyncio/SQLite, git-worktree isolation, role-manifest policy, evidence-gated merge, dashboard) is the foundation and does not get re-architected to fit this feature — the feature is built to fit it.

## Non-negotiable principles

1. **Test-first.** No production code without a failing test first. RED → GREEN → REFACTOR, every task. A task is not done until its test went RED→GREEN and `uv run pytest -q` is green for the whole suite, not just the new test.

2. **Inherit fleet-orchestrator's existing gates as a floor, not a ceiling.** `uv run pytest -q`, `ruff`, `mypy` must stay clean across the full repo (371+ tests) after every task. Never weaken or skip an existing test to make a new one pass.

3. **No invented business rules.** The hermes role graph's exact step sequence, assignees, and idempotency-key scheme are facts to port, not redesign. Where fleet-orchestrator's role-manifest schema can't represent something hermes does (e.g. a Hermes-Kanban-specific feature), mark it `Unknown` / `Escalate` in requirements.md — do not silently invent a substitute behavior.

4. **Security from line one.** No secrets in code or committed config. Any credential the Hermes Kanban adapter used must not leak into the new fleet-native path — confirm it's fully removed, not just unused.

5. **Verify in runtime, not just compile-clean.** "Done" means an actual end-to-end pipeline run (PM through Release) executes against fleet's spawn API and produces the evidence-gated merge artifacts — not just green unit tests in isolation.

6. **Sinks over pipes.** The pipeline template's stage-to-stage handoffs must be visible in fleet's existing event/inbox model (SSE events, inbox messages) — no new hidden side-channel between stages that an agent reading the code later would have to trace by hand.

7. **Minimal diff to fleet-orchestrator's existing modules.** Add the pipeline template as new, clearly-bounded code (e.g. a new module/role-manifest set) rather than modifying the existing agent lifecycle, workspace, policy, or merge services unless a task's acceptance criteria explicitly requires it.

8. **Archive, don't delete, the donor repos.** `hermes-ai-software-team-pipeline` and `harness-engineering` are archived (git tag + README pointer to the new location) only after the port is validated end-to-end. Never delete repo history as part of "cleanup."

9. **Grader ≠ doer.** Before declaring the port done, an independent pass (fresh subagent/session) attempts to prove the ported pipeline does NOT reproduce hermes's original step graph or does NOT run on fleet's engine. Default to "not done" until that refutation attempt fails.

## Inherited from global CLAUDE.md

- TDD is mandatory, no exceptions for this project.
- `uv` is the package manager (already fleet-orchestrator's tooling — no change).
- Minimal changes only — no drive-by refactors of fleet-orchestrator's existing services.
- State assumptions openly; name trade-offs instead of silently picking one.
