"""Pure-function planner for pipeline previews (zero I/O).

Ported from hermes-ai-software-team-pipeline's planner logic.
Produces a list of steps from an idea string and workflow definition.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from fleet.pipeline.models import Workflow


class EmptyIdeaError(ValueError):
    """Raised when idea text is empty or produces an empty slug.

    This can happen from empty input or after ASCII transliteration.
    """

    pass


@dataclass(frozen=True)
class PlannedStep:
    """A single planned step in a pipeline preview.

    Immutable (frozen=True) to represent read-only preview output.
    """

    step_key: str
    """Unique identifier for this step within the workflow."""

    title: str
    """The title for this step (after template substitution with idea)."""

    assignee: str
    """The agent/role assigned to execute this step."""

    workspace: str
    """Workspace type for execution (e.g., 'scratch' or 'worktree')."""

    idempotency_key: str
    """Preview-time idempotency key: pipeline:<slug>:<step_key>."""


def _make_slug(text: str) -> str:
    """Generate a kebab-case ASCII slug from text (max 40 chars, cut at word boundary).

    Ported verbatim from hermes's _make_slug.

    Args:
        text: The text to slugify.

    Returns:
        A kebab-case slug, max 40 chars, cut at word boundaries.

    Raises:
        EmptyIdeaError: If the resulting slug is empty after normalization.
    """
    # Normalize Unicode to decomposed form (NFKD)
    normalized = unicodedata.normalize("NFKD", text)

    # Remove non-ASCII characters
    ascii_text = normalized.encode("ascii", errors="ignore").decode("ascii")

    # Convert to lowercase and replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")

    # Truncate at 40 chars, cutting at word boundary
    if len(slug) > 40:
        truncated = slug[:40]
        last_hyphen = truncated.rfind("-")
        if last_hyphen != -1:
            truncated = truncated[:last_hyphen]
        slug = truncated.rstrip("-")

    if not slug:
        raise EmptyIdeaError(
            "Title produces an empty slug after ASCII transliteration."
        )

    return slug


def build_plan(idea: str, workflow: Workflow) -> list[PlannedStep]:
    """Generate a list of planned steps from an idea and workflow (pure, zero-I/O).

    Args:
        idea: The idea/prompt string describing what this plan is for.
        workflow: The Workflow template defining the task structure.

    Returns:
        A list of PlannedStep objects in workflow.tasks order.

    Raises:
        EmptyIdeaError: If idea is empty or whitespace-only.
    """
    # Strip and validate idea
    stripped_idea = idea.strip()
    if not stripped_idea:
        raise EmptyIdeaError("Idea must not be empty or whitespace-only.")

    # Generate slug
    slug = _make_slug(stripped_idea)

    # Build planned steps from workflow tasks
    steps: list[PlannedStep] = []
    for task_spec in workflow.tasks:
        # Format title using task template
        title = task_spec.title_tmpl.format(title=stripped_idea)

        # Create planned step
        step = PlannedStep(
            step_key=task_spec.step_key,
            title=title,
            assignee=task_spec.profile,
            workspace=task_spec.workspace,
            idempotency_key=f"pipeline:{slug}:{task_spec.step_key}",
        )
        steps.append(step)

    return steps
