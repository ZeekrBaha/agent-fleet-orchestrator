"""PipelineService — DAG-walk orchestration for multi-stage pipeline runs.

Public API:
    PipelineService.create_run(workflow, idea, scope) -> PipelineRun
    PipelineService.advance_run(run_id) -> PipelineRun

advance_run is a sink: it spawns every stage whose dependency edges are all
'passed' and which is itself 'pending', drives non-worktree stages to
completion synchronously (via a single scripted MockBackend turn), and
leaves worktree stages (impl/fix) 'running' for a later task's merge-gate
check. Calling it repeatedly walks the DAG forward one reachable frontier
at a time; calling it with nothing newly eligible is a no-op.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

from fleet.agents.backends.mock import MockBackend
from fleet.agents.backends.protocol import TextChunk, TurnEnd
from fleet.pipeline.models import PipelineRun, PipelineStage, RunStatus, StageStatus
from fleet.util.time import utcnow_iso

if TYPE_CHECKING:
    from fleet.agents.service import AgentService
    from fleet.approvals.service import ApprovalService
    from fleet.db import DatabaseManager
    from fleet.pipeline.models import Workflow
    from fleet.pipeline.repository import PipelineRepository
    from fleet.review.evidence import EvidenceService

_ROOT_ROLE = "orchestrator"
_TURN_TIMEOUT_S = 3.0
_NOT_READY_REASON = "No validation evidence recorded; run checks before merging"


def _single_turn_backend() -> MockBackend:
    """A MockBackend transcript that completes exactly one turn with no tools."""
    return MockBackend(
        transcript=[
            [
                TextChunk(text="done"),
                TurnEnd(cost_usd=0.0, input_tokens=1, output_tokens=1, context_pct=0.0),
            ]
        ]
    )


class PipelineService:
    """Orchestrates a Workflow's DAG on top of AgentService/PipelineRepository."""

    def __init__(
        self,
        db: DatabaseManager,
        repo: PipelineRepository,
        agent_service: AgentService,
        evidence_service: EvidenceService,
        approval_service: ApprovalService,
    ) -> None:
        self._db = db
        self._repo = repo
        self._agents = agent_service
        self._evidence = evidence_service
        self._approvals = approval_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_run(
        self, workflow: Workflow, idea: str, scope: str
    ) -> PipelineRun:
        """Create a PipelineRun plus one pending PipelineStage per workflow task."""
        run_id = str(uuid.uuid4())
        root = await self._agents.create_agent(
            scope=scope,
            name=f"pipeline-root-{run_id[:8]}",
            role=_ROOT_ROLE,
            backend=MockBackend(transcript=[]),
            model="mock",
            task_description=f"Pipeline run for: {idea}",
        )

        run = PipelineRun(
            id=run_id,
            workflow_name=workflow.name,
            idea=idea,
            scope=scope,
            root_agent_id=root.id,
            status=RunStatus.RUNNING,
            created_at=utcnow_iso(),
        )
        await self._repo.create_run(run)

        for task_spec in workflow.tasks:
            stage = PipelineStage(
                id=str(uuid.uuid4()),
                run_id=run.id,
                step_key=task_spec.step_key,
                role=task_spec.role,
                agent_id=None,
                task_id=None,
                idempotency_key=f"pipeline:{run.id}:{task_spec.step_key}",
                status=StageStatus.PENDING,
            )
            await self._repo.create_stage(stage)

        return run

    async def advance_run(self, run_id: str) -> PipelineRun:
        """Spawn every currently-eligible pending stage; drive scratch stages
        to completion synchronously; leave worktree stages 'running'."""
        run = await self._repo.get_run(run_id)
        if run is None:
            raise ValueError(f"No pipeline run with id {run_id!r}")

        if run.status == RunStatus.BLOCKED:
            # A prior call already routed a failure to the approval queue;
            # the DAG halts until a human resolves it. No-op.
            return run

        from fleet.pipeline.workflows import load as load_workflow

        workflow = load_workflow(run.workflow_name)
        tasks_by_key = {task.step_key: task for task in workflow.tasks}
        deps_by_step: dict[str, list[str]] = {
            task.step_key: [] for task in workflow.tasks
        }
        for parent, child in workflow.edges:
            deps_by_step[child].append(parent)

        # Pass 1: check the merge gate for any worktree stage already
        # 'running' with an evidence task attached -- mark it 'passed' if
        # the gate is open. Runs before the spawn pass so a stage this
        # unblocks (e.g. impl -> review) is visible within the same call.
        for stage in await self._repo.get_stages(run.id):
            task_spec = tasks_by_key[stage.step_key]
            if (
                task_spec.workspace == "worktree"
                and stage.status == StageStatus.RUNNING
                and stage.task_id is not None
            ):
                can_merge, reason = await self._evidence.check_merge_gate(
                    stage.task_id
                )
                if can_merge:
                    await self._repo.update_stage_status(
                        stage.id, StageStatus.PASSED
                    )
                elif reason != _NOT_READY_REASON:
                    # A real failure (failing check, stale evidence, missing
                    # reviewer verdict) -- not just "no evidence yet". Halt
                    # the DAG and route to the approval queue.
                    await self._repo.update_stage_status(
                        stage.id, StageStatus.FAILED
                    )
                    await self._approvals.request(
                        scope=run.scope,
                        agent_id=stage.agent_id or run.root_agent_id,
                        action=f"pipeline-stage-failed:{stage.step_key}",
                        description=(
                            f"Pipeline run {run.id} stage {stage.step_key!r}"
                            f" failed its merge gate: {reason}"
                        ),
                        metadata={"run_id": run.id, "step_key": stage.step_key},
                    )
                    await self._repo.update_run_status(run.id, RunStatus.BLOCKED)
                    return await self._repo.get_run(run.id)  # type: ignore[return-value]

        # Pass 2: spawn every currently-eligible pending stage. Re-fetch
        # stages after each spawn so a stage completed earlier in this same
        # pass is visible to later eligibility checks (fan-out then fan-in
        # within one call, per architecture.md).
        for task_spec in workflow.tasks:
            stages_by_key = {
                s.step_key: s for s in await self._repo.get_stages(run.id)
            }
            stage = stages_by_key[task_spec.step_key]
            if stage.status != StageStatus.PENDING:
                continue

            deps = deps_by_step[task_spec.step_key]
            if not all(
                stages_by_key[dep].status == StageStatus.PASSED for dep in deps
            ):
                continue

            title = task_spec.title_tmpl.format(title=run.idea)
            agent = await self._agents.create_agent(
                scope=run.scope,
                name=f"{run.id[:8]}-{task_spec.step_key}",
                role=task_spec.role,
                backend=_single_turn_backend(),
                model="mock",
                parent_id=run.root_agent_id,
                task_description=title,
            )
            if task_spec.workspace == "worktree":
                task_id = await self._evidence.create_task(
                    scope=run.scope,
                    title=title,
                    description=title,
                    owner_agent_id=agent.id,
                    branch=task_spec.branch,
                )
                await self._repo.update_stage_status(
                    stage.id,
                    StageStatus.RUNNING,
                    agent_id=agent.id,
                    task_id=task_id,
                )
                # Merge-gate check happens on a later advance_run call, once
                # evidence has been recorded against task_id.
                continue

            await self._repo.update_stage_status(
                stage.id, StageStatus.RUNNING, agent_id=agent.id
            )

            await self._agents.send_message(agent.id, "system", title)
            await self._wait_for_idle(agent.id)
            await self._repo.update_stage_status(stage.id, StageStatus.PASSED)

        return await self._repo.get_run(run.id)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _wait_for_idle(
        self, agent_id: str, timeout: float = _TURN_TIMEOUT_S
    ) -> None:
        """Poll until the agent's single scripted turn completes (status idle)."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            agent = await self._agents.get_agent(agent_id)
            if agent is not None and agent.status == "idle":
                return
            await asyncio.sleep(0.02)
        raise TimeoutError(
            f"Agent {agent_id!r} never returned to idle within {timeout}s"
        )
