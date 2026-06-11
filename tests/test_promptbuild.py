"""Tests for fleet/agents/promptbuild.py — written TDD-first (RED phase).

All tests must fail before the production code is created.
"""
from __future__ import annotations

import pytest  # noqa: F401 (used by pytest.raises)

from fleet.agents.promptbuild import (
    AssembledPrompt,
    MissingRolePromptError,
    PromptLayer,
    assemble_prompt,
    estimate_tokens,
    truncate_to_budget,
)

# ---------------------------------------------------------------------------
# Utility tests
# ---------------------------------------------------------------------------


def test_estimate_tokens_rounds_up() -> None:
    """ceil(5 / 4) == 2."""
    assert estimate_tokens("abcde") == 2


def test_estimate_tokens_exact_multiple() -> None:
    """ceil(4 / 4) == 1."""
    assert estimate_tokens("abcd") == 1


def test_estimate_tokens_empty() -> None:
    assert estimate_tokens("") == 0


def test_truncate_preserves_content_within_budget() -> None:
    """Short text should come back unchanged."""
    text = "hello world"
    result = truncate_to_budget(text, budget_tokens=100)
    assert result == text


def test_truncate_cuts_long_text() -> None:
    """Text whose token estimate exceeds the budget must be shortened."""
    # 4000 chars → 1000 tokens; budget is 5 tokens (20 chars)
    long_text = "word " * 800  # 4000 chars
    result = truncate_to_budget(long_text, budget_tokens=5)
    assert len(result) < len(long_text)
    assert estimate_tokens(result) <= 5


def test_truncate_no_cap_when_budget_zero() -> None:
    """budget_tokens == 0 means no cap; text is returned unchanged."""
    text = "x" * 10_000
    assert truncate_to_budget(text, budget_tokens=0) == text


# ---------------------------------------------------------------------------
# Core assembly tests
# ---------------------------------------------------------------------------


def test_assemble_prompt_contains_all_layers() -> None:
    """Assembled result has exactly 8 layers; platform rules appear in system_prompt."""
    result = assemble_prompt(
        role="orchestrator",
        task_prompt="Build a feature.",
        team_state="2 agents active.",
        memory_snippets=["Remember X."],
        tool_descriptions=["tool_a: does A"],
        workspace_context="repo=/repo",
    )
    assert isinstance(result, AssembledPrompt)
    assert len(result.layers) == 8
    # platform rules marker must be present
    assert "Fleet Platform Rules" in result.system_prompt


def test_missing_role_raises() -> None:
    """Requesting an unknown role must raise MissingRolePromptError (ADR-005)."""
    with pytest.raises(MissingRolePromptError):
        assemble_prompt(role="nonexistent_role", task_prompt="Do something.")


def test_platform_layer_within_budget() -> None:
    """Platform layer token_count must be ≤ 800."""
    result = assemble_prompt(role="orchestrator", task_prompt="Go.")
    platform_layer = result.layers[0]
    assert platform_layer.name == "platform"
    assert platform_layer.token_count <= 800


def test_role_layer_within_budget() -> None:
    """Orchestrator role layer token_count must be ≤ 1200."""
    result = assemble_prompt(role="orchestrator", task_prompt="Go.")
    role_layer = result.layers[2]
    assert role_layer.name == "role"
    assert role_layer.token_count <= 1200


def test_task_prompt_not_truncated() -> None:
    """A 10 000-char task prompt must survive unchanged in the assembled layers."""
    long_task = "A" * 10_000
    result = assemble_prompt(role="orchestrator", task_prompt=long_task)
    task_layer = result.layers[4]
    assert task_layer.name == "task"
    assert task_layer.content == long_task


def test_team_state_truncated_to_budget() -> None:
    """team_state longer than 600-token budget must be truncated."""
    # 600 tokens ≈ 2400 chars; use 5000 chars to ensure truncation
    long_state = "state " * 834  # ~5004 chars → ~1251 tokens
    result = assemble_prompt(
        role="orchestrator", task_prompt="Go.", team_state=long_state
    )
    team_layer = result.layers[5]
    assert team_layer.name == "team_state"
    assert team_layer.token_count <= 600


def test_memory_snippets_included() -> None:
    """Memory snippet text must appear in the assembled system_prompt."""
    snippet = "ADR-007: use event sourcing"
    result = assemble_prompt(
        role="orchestrator",
        task_prompt="Go.",
        memory_snippets=[snippet],
    )
    assert snippet in result.system_prompt


def test_tool_descriptions_included() -> None:
    """Tool description text must appear in the assembled system_prompt."""
    tool_desc = "spawn_worker: spawns a worker agent"
    result = assemble_prompt(
        role="orchestrator",
        task_prompt="Go.",
        tool_descriptions=[tool_desc],
    )
    assert tool_desc in result.system_prompt


def test_workspace_context_substituted() -> None:
    """workspace.md template placeholders must be substituted with context values."""
    result = assemble_prompt(
        role="orchestrator",
        task_prompt="Go.",
        workspace_context=(
            "repository_path=/repo default_branch=main"
            " worktree_branch=feat owned_paths=src/"
        ),
    )
    workspace_layer = result.layers[1]
    assert workspace_layer.name == "workspace"
    # Provided keys must be substituted; missing keys survive as {key} — acceptable.
    assert "{repository_path}" not in workspace_layer.content


def test_total_tokens_equals_sum_of_layers() -> None:
    """total_tokens on AssembledPrompt equals the sum of each layer's token_count."""
    result = assemble_prompt(role="coder", task_prompt="Implement the feature.")
    assert result.total_tokens == sum(layer.token_count for layer in result.layers)


def test_all_four_roles_load() -> None:
    """All four named roles must have prompt files and load without error."""
    for role in ("orchestrator", "coder", "reviewer", "observer"):
        result = assemble_prompt(role=role, task_prompt="Test.")
        assert len(result.layers) == 8, f"Role {role!r} did not produce 8 layers"


def test_empty_memory_and_tools_produce_empty_layers() -> None:
    """When memory and tools are empty/None, those layers exist with empty content."""
    result = assemble_prompt(role="observer", task_prompt="Watch.")
    memory_layer = result.layers[6]
    tools_layer = result.layers[7]
    assert memory_layer.name == "memory"
    assert tools_layer.name == "tools"
    # content should be empty string (no snippets provided)
    assert memory_layer.content == ""
    assert tools_layer.content == ""


def test_layer_separator_in_system_prompt() -> None:
    """Non-empty adjacent layers must be separated by the canonical separator."""
    result = assemble_prompt(role="orchestrator", task_prompt="Do it.")
    assert "\n\n---\n\n" in result.system_prompt


def test_prompt_layer_dataclass_fields() -> None:
    """PromptLayer has name, content, and token_count attributes."""
    layer = PromptLayer(name="test", content="hello", token_count=1)
    assert layer.name == "test"
    assert layer.content == "hello"
    assert layer.token_count == 1
