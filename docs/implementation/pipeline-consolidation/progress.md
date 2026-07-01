# Progress — Pipeline Consolidation

Live handoff log. Update after every task, not just at the end.

## Status: T1-T6 of 13 complete and merged to main. Continuing with T7.

## Done + verified
- constitution.md, research.md, requirements.md, architecture.md, implementation-plan.md, agent-assignments.md, validation-plan.md written.
- T1 (fleet/pipeline/models.py), T2 (fleet/pipeline/workflows.py), T3 (migration 0007 + fleet/pipeline/repository.py), T4 (fleet/pipeline/planner.py), T5 (8 new role-manifest entries), T6 (fleet/pipeline/service.py PipelineService.create_run/advance_run) — all merged to main, 420 tests passing, ruff/mypy clean.
- T1-T2 reviewed via subagent-driven-development's task-reviewer subagent. T3 onward reviewed inline by the controller after Agent-tool subagent dispatch became unreliable (see below).

## In flight
- None. Next: T7 (evidence + merge gate wiring for impl/fix stages) per implementation-plan.md.

## Blocked / unverified
- None currently. Historical blocker (resolved by working around it): the sandbox's auto-mode classifier repeatedly rejected Agent-tool subagent dispatches (both reviewer and implementer roles) starting partway through T2's review, citing the context-mode plugin's own auto-injected `<context_window_protection>` system-reminder as a suspected "oversight bypass." This isn't something injectable via prompt content — it's the harness's own plugin instructions. User chose to have the controller implement T5 onward directly (TDD, same rigor) rather than keep retrying subagent dispatch. Root cause likely fixable by disabling/reconfiguring the context-mode plugin, but not confirmed.

## Decisions / rejected approaches
- Rejected porting hermes's Jinja2 template system — reuse fleet's existing `fleet/prompts/roles/*.md` convention instead (requirements.md non-goals).
- Rejected designing a new queue/worker execution model — `PipelineService` stays sync-feeling async/await over SQLite WAL to match fleet's existing `AgentService` shape (architecture.md trade-off).
- Resolved research.md's 3 open questions via stated Decisions in requirements.md (worktree scope, failure handling, idempotency) — flagged as overridable, not escalated, per Auto Mode.
- T5 scope reduction: dropped the speculative `write_artifact` new-tool idea from architecture.md — `PolicyService.check_tool_allowed` only checks tool-name strings, so the 8 new roles reuse existing tool names (junior-dev = coder's list; senior-dev/junior-qa/senior-qa = reviewer's list) instead of requiring a new tool implementation.
- T6 discovery (plan gap, not in implementation-plan.md originally): `AgentService.create_agent` fails closed with `MissingRolePromptError` if `fleet/prompts/roles/<role>.md` doesn't exist for the given role. T5's plan didn't create these for its 8 new roles. Fixed by writing minimal role prompt files as part of T6 (first task that actually spawns these roles), rather than retroactively reopening T5.
- T6 design clarification: `advance_run()` processes the workflow's `tasks` tuple in order within a single call, so a fan-out (pm → ux, arch) and a subsequent fan-in (ux, arch → impl) can both resolve within one call if the earlier stages complete synchronously — this is a full "reachable frontier" walk per call, not "one stage per call." implementation-plan.md's T6 description was ambiguous on this; the controller resolved it this way and adjusted the task's tests accordingly.
- Root pipeline agent's name changed from `pipeline-root-{workflow_name}` to `pipeline-root-{run_id[:8]}` after a real bug surfaced in TDD: the `agents` table has `UNIQUE(scope, name)`, and a fixed name per workflow collided across multiple runs in the same scope.
