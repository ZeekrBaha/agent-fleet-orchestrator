# Agent Assignments — Pipeline Consolidation

Implementation plan: `./implementation-plan.md`. Reuses the repo's existing role prompts at `docs/prompts/{team-lead,developer,junior-developer,tester,reviewer}.md` — same pattern as the original build's `docs/implementation/agent-assignments.md`. No new prompt files for this initiative.

| Task | Owner prompt | Support |
|---|---|---|
| T1 Data model | developer.md | — |
| T2 Port FULL_SDLC | developer.md | tester.md (edge-tuple regression test is the AC — Tester owns asserting it matches research.md verbatim) |
| T3 Migration + repository | developer.md | reviewer.md (SQL migration review — repo has a history of numbered migrations, mandatory pattern check) |
| T4 Planner | junior-developer.md | developer.md (pure function, low risk, good junior task) |
| T5 Role manifests (+ T5a write_artifact tool if needed) | developer.md | reviewer.md (fail-closed audit — mandatory, matches original build's 4.2 Policy precedent) |
| T6 PipelineService happy path | developer.md | tester.md (DAG-order golden sequence, same shape as original build's 5.2 orchestration e2e) |
| T7 Evidence/merge gate wiring | developer.md | reviewer.md (mandatory — touches merge-gate semantics, matches original build's 6.1 precedent) |
| T8 Approval-queue failure routing | junior-developer.md | developer.md |
| T9 SSE events | junior-developer.md | — |
| T10 API routes | developer.md | tester.md (request/response schema fixtures) |
| T11 Full e2e integration test | tester.md (owns this task outright — it's a test, not a feature) | reviewer.md (this is also the independent-refutation surface; see validation-plan.md) |
| T12 SOP fold-in | junior-developer.md | — (docs-only, no reviewer gate needed) |
| T13 Donor repo archival | team-lead.md | — (manual/process step, not a coding task) |

## Rules (inherited from original build's assignment doc, unchanged)

- Team Lead opens each task with scope + AC excerpt from `implementation-plan.md`, closes it only after the owner reports tests/lint/typecheck green and Reviewer signs where listed.
- Reviewer pass is mandatory before T5, T7, and T11 — these are the three tasks that touch fail-closed policy, merge-gate semantics, or serve as the refutation gate.
- Tester owns T2's regression assertion and T11's e2e test outright; may reject any task's output as untestable per constitution principle 1 (no task is "done" without a test, T12/T13 excepted as documented).
- No agent works outside its task's "Files" list (per implementation-plan.md) without Team Lead sign-off — this is what keeps T1–T13 a sequence of small diffs against `agent-fleet-orchestrator`, not a refactor.
