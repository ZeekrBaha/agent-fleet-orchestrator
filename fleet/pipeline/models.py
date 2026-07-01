"""Data models for fleet pipeline/workflow system.

Pure data layer — no I/O, no dependencies on other fleet subpackages.
Defines: TaskSpec, Workflow, PipelineRun, PipelineStage, and status enums.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class StageStatus(StrEnum):
    """Status of a single pipeline stage.

    Values must match SQL schema:
    CHECK (status IN ('pending','running','passed','failed'))
    """

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class RunStatus(StrEnum):
    """Status of an overall pipeline run.

    Values must match SQL schema: CHECK (status IN ('running','blocked','done'))
    """

    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"


@dataclass(frozen=True)
class TaskSpec:
    """Specification of a single task in a workflow.

    Immutable (frozen=True) because it represents a template/blueprint that is
    ported near-verbatim from hermes's FULL_SDLC workflow in T2.

    Fields match hermes's original TaskSpec shape (research.md).
    """

    step_key: str
    """Unique identifier for this step within the workflow."""

    title_tmpl: str
    """Template for the task title (may contain variables)."""

    profile: str
    """Agent profile/capability required (e.g., 'architect', 'engineer')."""

    role: str
    """Role/persona for the agent executing this task."""

    template: str | None
    """Path to or name of a task template; None if not templated."""

    workspace: str
    """Workspace type: e.g., 'scratch' or 'worktree'."""

    branch: str | None
    """Git branch to use; None if not branch-specific."""


@dataclass(frozen=True)
class Workflow:
    """A complete workflow definition comprising multiple tasks and their edges.

    Immutable (frozen=True) as it is a template/blueprint.
    """

    name: str
    """Name of the workflow (e.g., 'FULL_SDLC')."""

    tasks: tuple[TaskSpec, ...]
    """Tuple of TaskSpec objects that make up this workflow."""

    edges: tuple[tuple[str, str], ...]
    """Tuple of (from_step_key, to_step_key) edges defining the DAG."""


@dataclass
class PipelineRun:
    """Represents a single execution of a workflow.

    Mutable (plain @dataclass) because it represents a live execution that
    gets updated as stages progress.
    """

    id: str
    """Unique identifier for this pipeline run."""

    workflow_name: str
    """Name of the workflow being executed."""

    idea: str
    """The idea/prompt/description for what this run is trying to achieve."""

    scope: str
    """Scope/project context for this run."""

    root_agent_id: str
    """ID of the root/coordinator agent orchestrating this run."""

    status: RunStatus
    """Current status of the overall pipeline run."""

    created_at: str
    """ISO 8601 timestamp of when the run was created."""


@dataclass
class PipelineStage:
    """Represents the execution of a single task within a pipeline run.

    Mutable (plain @dataclass) because it tracks live execution state.
    """

    id: str
    """Unique identifier for this stage."""

    run_id: str
    """ID of the PipelineRun this stage belongs to."""

    step_key: str
    """The step_key of the TaskSpec being executed."""

    role: str
    """The role being used for this stage execution."""

    agent_id: str | None
    """ID of the agent executing this stage; None if not yet assigned."""

    task_id: str | None
    """ID of the underlying task/job; None if not yet created."""

    idempotency_key: str
    """Key used for idempotency (prevents duplicate executions)."""

    status: StageStatus
    """Current status of this stage."""
