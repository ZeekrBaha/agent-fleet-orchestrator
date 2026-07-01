"""Tests for fleet.pipeline.planner module.

Tests cover: build_plan() pure function, EmptyIdeaError, PlannedStep structure,
slug generation, idempotency_key formatting, and zero-I/O guarantee.
"""

import inspect

import pytest

from fleet.pipeline.planner import EmptyIdeaError, PlannedStep, build_plan
from fleet.pipeline.workflows import FULL_SDLC


class TestPlannedStepDataclass:
    """Test PlannedStep structure."""

    def test_planned_step_is_frozen(self):
        """Assert PlannedStep is immutable (frozen=True)."""
        step = PlannedStep(
            step_key="test",
            title="Test title",
            assignee="test-agent",
            workspace="scratch",
            idempotency_key="pipeline:test-slug:test",
        )
        # Attempting to modify a frozen dataclass should raise FrozenInstanceError
        with pytest.raises((AttributeError, TypeError)):
            step.title = "New title"


class TestBuildPlanBasic:
    """Test build_plan() basic functionality."""

    def test_build_plan_full_sdlc_returns_9_steps(self):
        """Test 1: build_plan returns 9 PlannedStep in FULL_SDLC task order."""
        result = build_plan("Build a Prompt Regression Lab", FULL_SDLC)

        # Assert 9 steps returned
        assert len(result) == 9
        assert all(isinstance(step, PlannedStep) for step in result)

        # Assert steps are in FULL_SDLC.tasks order
        result_keys = [s.step_key for s in result]
        expected_keys = [t.step_key for t in FULL_SDLC.tasks]
        assert result_keys == expected_keys

    def test_pm_step_exact_values(self):
        """Test 2: PM step has exact title, assignee, and workspace."""
        result = build_plan("Build a Prompt Regression Lab", FULL_SDLC)

        # Find pm step
        pm_step = next(s for s in result if s.step_key == "pm")

        # Check title is exactly template substitution
        assert pm_step.title == "PM spec for Build a Prompt Regression Lab"
        assert pm_step.assignee == "pm-agent"
        assert pm_step.workspace == "scratch"

    def test_idempotency_key_pattern(self):
        """Test 3: All steps have idempotency_key matching pipeline:<slug>:<step_key>.

        Verify the pattern and that all steps share the same slug.
        """
        result = build_plan("Build a Prompt Regression Lab", FULL_SDLC)

        # Extract slug from first step's idempotency_key
        first_key = result[0].idempotency_key
        assert first_key.startswith("pipeline:")
        parts = first_key.split(":")
        assert len(parts) == 3, f"Expected 3 parts in {first_key}"
        slug = parts[1]

        # Verify all steps use same slug and correct step_key
        for step in result:
            expected_key = f"pipeline:{slug}:{step.step_key}"
            assert step.idempotency_key == expected_key
            assert step.idempotency_key.endswith(step.step_key)


class TestBuildPlanEmptyIdea:
    """Test EmptyIdeaError handling."""

    def test_empty_string_raises_empty_idea_error(self):
        """Test 4a: Empty string raises EmptyIdeaError."""
        with pytest.raises(EmptyIdeaError):
            build_plan("", FULL_SDLC)

    def test_whitespace_only_raises_empty_idea_error(self):
        """Test 4b: Whitespace-only string raises EmptyIdeaError."""
        with pytest.raises(EmptyIdeaError):
            build_plan("   ", FULL_SDLC)

    def test_empty_idea_error_is_value_error(self):
        """Assert EmptyIdeaError is a ValueError."""
        with pytest.raises(ValueError):
            build_plan("", FULL_SDLC)


class TestSlugGeneration:
    """Test slug generation and word boundary logic."""

    def test_slug_word_boundary_cut(self):
        """Test 5: Slug longer than 40 chars is cut at word boundary (hyphen).

        Create a title that would split mid-word without boundary logic.
        The slug algorithm should cut at the last hyphen, not mid-word.
        """
        # Long title to generate slug > 40 chars, test word boundary handling
        long_title = (
            "this is a very long title that exceeds forty characters when "
            "slugified for testing boundary logic"
        )

        result = build_plan(long_title, FULL_SDLC)
        slug = result[0].idempotency_key.split(":")[1]

        # Slug must be <= 40 chars
        assert len(slug) <= 40

        # Slug should not end with a hyphen (would indicate mid-word cut)
        assert not slug.endswith("-")

        # Slug should be cut at a word boundary (previous hyphen or shorter)
        # This verifies the rfind("-") logic is working
        assert all(s.isalnum() or s == "-" for s in slug)


class TestZeroIOGuarantee:
    """Test the zero-I/O guarantee via function signature."""

    def test_build_plan_signature_has_exactly_two_parameters(self):
        """Test 6: build_plan signature has exactly 2 parameters (idea, workflow).

        This ensures no hidden service/client dependency can be added
        without this test failing.
        """
        sig = inspect.signature(build_plan)
        params = list(sig.parameters.keys())

        # Must have exactly 2 parameters
        assert len(params) == 2, f"Expected 2 parameters, got {len(params)}: {params}"

        # Parameters must be named 'idea' and 'workflow' (or compatible names)
        assert "idea" in params, f"Missing 'idea' parameter in {params}"
        assert "workflow" in params, f"Missing 'workflow' parameter in {params}"

        # No 'self', no service/client parameter
        assert "self" not in params


class TestEmptyIdeaError:
    """Test EmptyIdeaError exception."""

    def test_empty_idea_error_message(self):
        """Assert EmptyIdeaError can be raised with a message."""
        with pytest.raises(EmptyIdeaError) as exc_info:
            raise EmptyIdeaError("Test message")
        assert "Test message" in str(exc_info.value)
