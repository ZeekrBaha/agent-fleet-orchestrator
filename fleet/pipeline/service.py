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
    from fleet.db import DatabaseManager
    from fleet.pipeline.models import Workflow
    from fleet.pipeline.repository import PipelineRepository

_ROOT_ROLE = "orchestrator"
_TURN_TIMEOUT_S = 3.0


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
    ) -> None:
        self._db = db
        self._repo = repo
        self._agents = agent_service

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

        from fleet.pipeline.workflows import load as load_workflow

        workflow = load_workflow(run.workflow_name)
        deps_by_step: dict[str, list[str]] = {
            task.step_key: [] for task in workflow.tasks
        }
        for parent, child in workflow.edges:
            deps_by_step[child].append(parent)

        # Re-fetch stages after each spawn so a stage completed earlier in
        # this same pass is visible to later eligibility checks (fan-out
        # then fan-in within one call, per architecture.md).
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
            await self._repo.update_stage_status(
                stage.id, StageStatus.RUNNING, agent_id=agent.id
            )

            if task_spec.workspace == "worktree":
                # Merge-gate check is wired in a later task; leave running.
                continue

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
