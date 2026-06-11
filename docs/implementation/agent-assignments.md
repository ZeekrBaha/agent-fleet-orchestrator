# Agent Assignments

Date: 2026-06-10
Status: Approved

How to run the build with role agents. One agent per role prompt; Team Lead sequences.

| Phase / Task | Owner prompt | Support |
|---|---|---|
| 1.1 Scaffold + config | developer.md | — |
| 1.2 DB + writer | developer.md | tester.md (load test) |
| 1.3 Events + SSE | developer.md | — |
| 2.1 Protocol + Mock | developer.md | tester.md (transcript fixtures) |
| 2.2 Lifecycle + inbox | developer.md | reviewer.md (state machine review) |
| 2.3 Claude adapter | developer.md | — (live smoke gated by Team Lead) |
| 2.4 Budgets | junior-developer.md | developer.md |
| 3.1 Repo registry + fixture | tester.md (fixture) → developer.md | — |
| 3.2 Worktrees + gates | developer.md | reviewer.md (git-safety review) |
| 4.1 Tool server | developer.md | — |
| 4.2 Policy | developer.md | reviewer.md (fail-closed audit) |
| 5.1 Prompt builder | developer.md | — |
| 5.2 Orchestration e2e | developer.md | tester.md (golden sequence) |
| 5.3 Compaction + memory | developer.md | — |
| 6.1 Merge gate | developer.md | reviewer.md (mandatory) |
| 6.2 Approvals | junior-developer.md | developer.md |
| 6.3 Reviewer role | developer.md | — |
| 7.1 Dashboard | developer.md + junior-developer.md | tester.md (Playwright + visual gate) |
| 7.2 Ops hardening | junior-developer.md | — |

Rules:
- Team Lead (team-lead.md) opens each task with scope + AC excerpt, closes it only after the
  owner reports tests/lint/typecheck and Reviewer signs where listed.
- Reviewer pass is mandatory before any phase boundary.
- Tester owns fixtures and may reject untestable task output.
- No agent works outside its task's "Files likely touched" without Team Lead sign-off.
