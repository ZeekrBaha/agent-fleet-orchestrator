"""Layered, budgeted prompt builder for Fleet agents.

Assembles a system prompt from 7 ordered layers, each with an optional
token budget.  Missing role prompts raise MissingRolePromptError (ADR-005).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# budget_tokens == 0 means no cap
TOKEN_BUDGETS: dict[str, int] = {
    "platform": 800,
    "workspace": 400,
    "role": 1200,
    "task": 0,        # no cap
    "team_state": 600,
    "memory": 800,
    "tools": 0,       # no cap
}

LAYER_SEPARATOR = "\n\n---\n\n"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PromptLayer:
    name: str
    content: str
    token_count: int  # estimated: ceil(len(content) / 4)


@dataclass
class AssembledPrompt:
    layers: list[PromptLayer]
    system_prompt: str   # all layers joined by LAYER_SEPARATOR (empty layers omitted)
    total_tokens: int    # sum of layer token_counts


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class MissingRolePromptError(Exception):
    """Raised when a role prompt file is absent (ADR-005 fail-closed)."""


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ceil(len(text) / 4).

    Uses integer ceiling division; returns 0 for empty string.
    """
    if not text:
        return 0
    return math.ceil(len(text) / 4)


def truncate_to_budget(text: str, budget_tokens: int) -> str:
    """Trim *text* so that estimate_tokens(result) <= budget_tokens.

    - If budget_tokens == 0, returns text unchanged (no cap).
    - Tries to truncate at a word boundary first; falls back to a hard character cut.
    - If the text already fits, returns it unchanged.
    """
    if budget_tokens == 0:
        return text
    if estimate_tokens(text) <= budget_tokens:
        return text

    max_chars = budget_tokens * 4  # upper bound for the target length

    # Try word-boundary truncation
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]

    # Verify the trim fits; if not (unlikely with the formula), do a hard cut
    while estimate_tokens(truncated) > budget_tokens and len(truncated) > 0:
        truncated = truncated[: len(truncated) - 1]

    return truncated


def load_role_prompt(role: str) -> str:
    """Load fleet/prompts/roles/<role>.md.

    Raises:
        MissingRolePromptError: if the file does not exist (ADR-005, fail-closed).
    """
    role_path = PROMPTS_DIR / "roles" / f"{role}.md"
    if not role_path.exists():
        raise MissingRolePromptError(
            f"Role prompt file not found: {role_path} "
            f"(role={role!r}). Add fleet/prompts/roles/{role}.md to fix."
        )
    return role_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _KeepMissingKeys(dict[str, str]):
    """Mapping that returns '{key}' for missing keys so format_map never raises."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


# ---------------------------------------------------------------------------
# Main assembler
# ---------------------------------------------------------------------------


def assemble_prompt(
    *,
    role: str,
    task_prompt: str,
    team_state: str = "",
    memory_snippets: list[str] | None = None,
    tool_descriptions: list[str] | None = None,
    workspace_context: str = "",
    prior_context: str = "",
) -> AssembledPrompt:
    """Build the layered system prompt.

    Layer order:
        0. platform      — fleet/prompts/platform.md          (≤ 800 tokens)
        1. workspace     — fleet/prompts/workspace.md          (≤ 400 tokens)
        2. role          — fleet/prompts/roles/<role>.md       (≤ 1200 tokens)
        3. prior_context — prior compaction summary (no cap; omitted if empty)
        4. task          — task_prompt (no cap)
        5. team_state    — generated snapshot (≤ 600 tokens)
        6. memory        — memory_snippets joined (≤ 800 tokens)
        7. tools         — tool_descriptions joined (no cap)

    Args:
        prior_context: Optional summary from a previous compaction cycle.
            When non-empty, injected after the role layer so the model resumes
            with its prior summary rather than starting from a blank slate.

    Returns:
        AssembledPrompt with all layers, system_prompt string, and total_tokens.

    Raises:
        MissingRolePromptError: if the role prompt file does not exist (ADR-005).
    """
    # -- Layer 0: platform rules -------------------------------------------------
    platform_raw = (PROMPTS_DIR / "platform.md").read_text(encoding="utf-8")
    platform_content = truncate_to_budget(platform_raw, TOKEN_BUDGETS["platform"])

    # -- Layer 1: workspace context ----------------------------------------------
    workspace_raw = (PROMPTS_DIR / "workspace.md").read_text(encoding="utf-8")
    # Substitute provided key=value pairs from workspace_context string.
    # Use format_map so unknown keys remain as {key} literals — acceptable per spec.
    context_dict: dict[str, str] = {}
    for pair in workspace_context.split():
        if "=" in pair:
            k, _, v = pair.partition("=")
            context_dict[k] = v
    # Use a defaultdict-like mapping so unknown keys stay as {key} literals
    # rather than raising KeyError — this is the "acceptable" behaviour per spec.
    workspace_filled = workspace_raw.format_map(_KeepMissingKeys(context_dict))
    workspace_content = truncate_to_budget(workspace_filled, TOKEN_BUDGETS["workspace"])

    # -- Layer 2: role prompt (hard error if missing) ----------------------------
    role_raw = load_role_prompt(role)  # raises MissingRolePromptError if absent
    role_content = truncate_to_budget(role_raw, TOKEN_BUDGETS["role"])

    # -- Layer 3: prior context from compaction (no cap; empty → omitted) --------
    prior_content = (
        "## Prior context (from compaction)\n\n" + prior_context
        if prior_context
        else ""
    )

    # -- Layer 4: task prompt (no cap) -------------------------------------------
    task_content = task_prompt  # explicitly no truncation

    # -- Layer 5: team state (≤ 600 tokens) -------------------------------------
    team_content = truncate_to_budget(team_state, TOKEN_BUDGETS["team_state"])

    # -- Layer 6: memory snippets (≤ 800 tokens) ---------------------------------
    memory_raw = "\n".join(memory_snippets) if memory_snippets else ""
    memory_content = truncate_to_budget(memory_raw, TOKEN_BUDGETS["memory"])

    # -- Layer 7: tool descriptions (no cap) ------------------------------------
    tools_content = "\n".join(tool_descriptions) if tool_descriptions else ""

    # -- Build PromptLayer objects -----------------------------------------------
    def _layer(name: str, content: str) -> PromptLayer:
        return PromptLayer(name, content, estimate_tokens(content))

    layers: list[PromptLayer] = [
        _layer("platform",      platform_content),
        _layer("workspace",     workspace_content),
        _layer("role",          role_content),
        _layer("prior_context", prior_content),
        _layer("task",          task_content),
        _layer("team_state",    team_content),
        _layer("memory",        memory_content),
        _layer("tools",         tools_content),
    ]

    # -- Assemble system prompt (skip empty layers to avoid stray separators) ----
    non_empty = [layer.content for layer in layers if layer.content]
    system_prompt = LAYER_SEPARATOR.join(non_empty)

    total_tokens = sum(layer.token_count for layer in layers)

    return AssembledPrompt(
        layers=layers,
        system_prompt=system_prompt,
        total_tokens=total_tokens,
    )
