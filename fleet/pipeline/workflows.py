"""SDLC workflow definition.

Ported from hermes-ai-software-team-pipeline's workflow.py, using TaskSpec and
Workflow classes from fleet.pipeline.models.
"""

from __future__ import annotations

from fleet.pipeline.models import TaskSpec, Workflow

FULL_SDLC = Workflow(
    name="full-sdlc",
    tasks=(
        TaskSpec(
            step_key="pm",
            title_tmpl="PM spec for {title}",
            profile="pm-agent",
            workspace="scratch",
            branch=None,
            template="pm_spec.md.j2",
            role="pm",
        ),
        TaskSpec(
            step_key="ux",
            title_tmpl="UX/product design for {title}",
            profile="ux-designer-agent",
            workspace="scratch",
            branch=None,
            template="ux_design.md.j2",
            role="ux",
        ),
        TaskSpec(
            step_key="arch",
            title_tmpl="Architecture plan for {title}",
            profile="architect-agent",
            workspace="scratch",
            branch=None,
            template="architecture.md.j2",
            role="architect",
        ),
        TaskSpec(
            step_key="impl",
            title_tmpl="Implement MVP for {title}",
            profile="junior-dev-agent",
            workspace="worktree",
            branch="wt/{slug}-impl",
            template="impl_task.md.j2",
            role="junior-dev",
        ),
        TaskSpec(
            step_key="review",
            title_tmpl="Senior dev review for {title}",
            profile="senior-dev-reviewer",
            workspace="scratch",
            branch=None,
            template="senior_dev_review.md.j2",
            role="senior-dev",
        ),
        TaskSpec(
            step_key="fix",
            title_tmpl="Fix review findings for {title}",
            profile="junior-dev-agent",
            workspace="worktree",
            branch="wt/{slug}-impl",
            template="impl_task.md.j2",
            role="junior-dev",
        ),
        TaskSpec(
            step_key="jqa",
            title_tmpl="Junior QA test pass for {title}",
            profile="junior-qa-agent",
            workspace="scratch",
            branch=None,
            template="junior_qa_report.md.j2",
            role="junior-qa",
        ),
        TaskSpec(
            step_key="sqa",
            title_tmpl="Senior QA audit for {title}",
            profile="senior-qa-agent",
            workspace="scratch",
            branch=None,
            template="senior_qa_audit.md.j2",
            role="senior-qa",
        ),
        TaskSpec(
            step_key="handoff",
            title_tmpl="Final handoff + README polish for {title}",
            profile="release-agent",
            workspace="scratch",
            branch=None,
            template="handoff.md.j2",
            role="release",
        ),
    ),
    edges=(
        ("pm", "ux"),
        ("pm", "arch"),
        ("ux", "impl"),
        ("arch", "impl"),
        ("impl", "review"),
        ("review", "fix"),
        ("fix", "jqa"),
        ("jqa", "sqa"),
        ("sqa", "handoff"),
    ),
)


def load(name: str) -> Workflow:
    """Return the Workflow for the given name.

    Args:
        name: The workflow name to load.

    Returns:
        The Workflow object.

    Raises:
        ValueError: If the workflow name is not recognized.
    """
    if name == "full-sdlc":
        return FULL_SDLC
    raise ValueError(f"Unknown workflow: {name!r}")
