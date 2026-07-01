"""Tests for fleet.pipeline.workflows module.

Tests cover: FULL_SDLC workflow structure, task uniqueness, workspace/branch values,
and the load() helper function.
"""

import pytest

from fleet.pipeline.workflows import FULL_SDLC, load


class TestFullSdlcWorkflow:
    """Test suite for the FULL_SDLC workflow definition."""

    def test_full_sdlc_edges_exact_match(self):
        """Hard-coded regression guard: verify edge tuple is byte-for-byte identical."""
        expected_edges = (
            ("pm", "ux"),
            ("pm", "arch"),
            ("ux", "impl"),
            ("arch", "impl"),
            ("impl", "review"),
            ("review", "fix"),
            ("fix", "jqa"),
            ("jqa", "sqa"),
            ("sqa", "handoff"),
        )
        assert FULL_SDLC.edges == expected_edges

    def test_full_sdlc_name_and_task_count(self):
        """Assert workflow name and task count."""
        assert FULL_SDLC.name == "full-sdlc"
        assert len(FULL_SDLC.tasks) == 9

    def test_full_sdlc_task_step_keys_unique_and_complete(self):
        """Assert every step_key is unique and matches the expected set."""
        step_keys = [task.step_key for task in FULL_SDLC.tasks]
        expected_keys = {
            "pm",
            "ux",
            "arch",
            "impl",
            "review",
            "fix",
            "jqa",
            "sqa",
            "handoff",
        }

        # Check all keys are unique
        unique_count = len(set(step_keys))
        assert len(step_keys) == unique_count, (
            f"Duplicate step_keys found: {step_keys}"
        )

        # Check all keys match expected set
        assert set(step_keys) == expected_keys, (
            f"Expected {expected_keys}, got {set(step_keys)}"
        )

    def test_full_sdlc_workspace_and_branch_values(self):
        """Assert workspace and branch values for impl, fix, and scratch tasks."""
        tasks_by_key = {task.step_key: task for task in FULL_SDLC.tasks}

        # impl and fix tasks should use worktree with specific branch
        assert tasks_by_key["impl"].workspace == "worktree"
        assert tasks_by_key["impl"].branch == "wt/{slug}-impl"

        assert tasks_by_key["fix"].workspace == "worktree"
        assert tasks_by_key["fix"].branch == "wt/{slug}-impl"

        # pm task (scratch task) should have no branch
        assert tasks_by_key["pm"].workspace == "scratch"
        assert tasks_by_key["pm"].branch is None

    def test_load_function(self):
        """Assert load() returns correct workflow and raises ValueError for unknowns."""
        # load("full-sdlc") should return the FULL_SDLC workflow
        loaded = load("full-sdlc")
        assert loaded is FULL_SDLC or loaded == FULL_SDLC

        # load() with unknown name should raise ValueError
        with pytest.raises(ValueError) as exc_info:
            load("nonexistent-workflow")
        assert "Unknown workflow" in str(exc_info.value)
        assert "nonexistent-workflow" in str(exc_info.value)
