"""Tests for fleet.pipeline.models — pure data layer for pipeline/workflow system."""

from __future__ import annotations

import ast
from pathlib import Path

from fleet.pipeline.models import (
    PipelineRun,
    PipelineStage,
    RunStatus,
    StageStatus,
    TaskSpec,
    Workflow,
)


class TestTaskSpecAndWorkflow:
    """Test TaskSpec and Workflow construction and round-tripping."""

    def test_workflow_with_two_tasks_and_one_edge(self) -> None:
        """Construct a Workflow with 2 TaskSpecs and 1 edge; all fields round-trip."""
        task1 = TaskSpec(
            step_key="design",
            title_tmpl="Design Phase",
            profile="architect",
            role="designer",
            template="design_template",
            workspace="scratch",
            branch="design-branch",
        )
        task2 = TaskSpec(
            step_key="implement",
            title_tmpl="Implementation Phase",
            profile="engineer",
            role="engineer",
            template="impl_template",
            workspace="worktree",
            branch=None,
        )
        workflow = Workflow(
            name="test_workflow",
            tasks=(task1, task2),
            edges=(("design", "implement"),),
        )

        # Assert all fields round-trip correctly
        assert workflow.name == "test_workflow"
        assert workflow.tasks == (task1, task2)
        assert workflow.edges == (("design", "implement"),)
        assert len(workflow.tasks) == 2

        # Assert TaskSpec fields
        assert workflow.tasks[0].step_key == "design"
        assert workflow.tasks[0].title_tmpl == "Design Phase"
        assert workflow.tasks[0].profile == "architect"
        assert workflow.tasks[0].role == "designer"
        assert workflow.tasks[0].template == "design_template"
        assert workflow.tasks[0].workspace == "scratch"
        assert workflow.tasks[0].branch == "design-branch"

        assert workflow.tasks[1].step_key == "implement"
        assert workflow.tasks[1].title_tmpl == "Implementation Phase"
        assert workflow.tasks[1].profile == "engineer"
        assert workflow.tasks[1].role == "engineer"
        assert workflow.tasks[1].template == "impl_template"
        assert workflow.tasks[1].workspace == "worktree"
        assert workflow.tasks[1].branch is None


class TestStageStatus:
    """Test StageStatus enum."""

    def test_stage_status_has_exactly_four_members(self) -> None:
        """Assert StageStatus has the 4 members: pending, running, passed, failed."""
        expected_values = {"pending", "running", "passed", "failed"}
        actual_values = {s.value for s in StageStatus}
        assert actual_values == expected_values, (
            f"Expected StageStatus values {expected_values}, got {actual_values}"
        )
        assert len(list(StageStatus)) == 4


class TestRunStatus:
    """Test RunStatus enum."""

    def test_run_status_has_exactly_three_members(self) -> None:
        """Assert RunStatus has exactly the 3 members: running, blocked, done."""
        expected_values = {"running", "blocked", "done"}
        actual_values = {s.value for s in RunStatus}
        assert actual_values == expected_values, (
            f"Expected RunStatus values {expected_values}, got {actual_values}"
        )
        assert len(list(RunStatus)) == 3


class TestPipelineRun:
    """Test PipelineRun dataclass."""

    def test_pipeline_run_construction(self) -> None:
        """Construct a PipelineRun and assert all fields are stored correctly."""
        run = PipelineRun(
            id="run-001",
            workflow_name="test_workflow",
            idea="Test idea for workflow",
            scope="test_scope",
            root_agent_id="agent-001",
            status=RunStatus.RUNNING,
            created_at="2026-06-30T12:00:00Z",
        )

        assert run.id == "run-001"
        assert run.workflow_name == "test_workflow"
        assert run.idea == "Test idea for workflow"
        assert run.scope == "test_scope"
        assert run.root_agent_id == "agent-001"
        assert run.status == RunStatus.RUNNING
        assert run.created_at == "2026-06-30T12:00:00Z"


class TestPipelineStage:
    """Test PipelineStage dataclass."""

    def test_pipeline_stage_construction(self) -> None:
        """Construct a PipelineStage and assert all fields are stored correctly."""
        stage = PipelineStage(
            id="stage-001",
            run_id="run-001",
            step_key="design",
            role="designer",
            agent_id="agent-001",
            task_id="task-001",
            idempotency_key="idempotency-key-001",
            status=StageStatus.RUNNING,
        )

        assert stage.id == "stage-001"
        assert stage.run_id == "run-001"
        assert stage.step_key == "design"
        assert stage.role == "designer"
        assert stage.agent_id == "agent-001"
        assert stage.task_id == "task-001"
        assert stage.idempotency_key == "idempotency-key-001"
        assert stage.status == StageStatus.RUNNING


class TestImportBoundary:
    """Test that models.py has no imports from other fleet subpackages."""

    def test_models_no_forbidden_imports(self) -> None:
        """Assert models.py does not import from forbidden subpackages."""
        models_path = (
            Path(__file__).parent.parent.parent
            / "fleet"
            / "pipeline"
            / "models.py"
        )
        assert models_path.exists(), f"Expected {models_path} to exist"

        source = models_path.read_text()
        tree = ast.parse(source)

        forbidden_modules = {
            "fleet.agents",
            "fleet.review",
            "fleet.api",
            "fleet.approvals",
            "fleet.events",
            "fleet.workspace",
            "fleet.toolserver",
        }

        found_forbidden = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_modules:
                        found_forbidden.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module in forbidden_modules:
                    found_forbidden.add(node.module)

        assert (
            not found_forbidden
        ), f"Found forbidden imports in models.py: {found_forbidden}"
