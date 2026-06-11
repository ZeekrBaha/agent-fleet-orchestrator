"""B3: Async hygiene — no bare sync git calls on the event loop.

TDD RED phase: these tests fail while sync calls exist, pass after wrapping.

Behaviors verified (all via source inspection):
1. worktree_service.py: is_repo_dirty, worktree_add, worktree_remove,
   get_worktree_status are not called directly — only via asyncio.to_thread.
2. worktree_service.py handle_dirty_repo: bare git_run calls replaced with
   asyncio.to_thread(git_run, ...) or an async wrapper.
3. workspace/service.py: detect_default_branch not called directly in async context.
4. tool_handlers.py: ConflictChecker().check() not called directly; wrapped in
   asyncio.to_thread.
5. asyncio.to_thread is actually imported/used in worktree_service and tool_handlers.
"""
from __future__ import annotations

from pathlib import Path

FLEET = Path(__file__).parent.parent / "fleet"
WS_SVC = FLEET / "workspace" / "worktree_service.py"
REPO_SVC = FLEET / "workspace" / "service.py"
TOOL_H = FLEET / "api" / "tool_handlers.py"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1-4. worktree_service.py: bare sync gitops calls must be gone
# ---------------------------------------------------------------------------


def test_is_repo_dirty_not_called_directly() -> None:
    """is_repo_dirty( must not appear as a direct call in worktree_service.py.

    After B3 the call must be: await asyncio.to_thread(is_repo_dirty, ...)
    """
    src = _src(WS_SVC)
    assert "is_repo_dirty(" not in src, (
        "is_repo_dirty( is a blocking sync call; wrap with asyncio.to_thread."
    )


def test_worktree_add_not_called_directly() -> None:
    """worktree_add( must not appear as a direct call in worktree_service.py."""
    src = _src(WS_SVC)
    assert "worktree_add(" not in src, (
        "worktree_add( is a blocking sync call; wrap with asyncio.to_thread."
    )


def test_worktree_remove_not_called_directly() -> None:
    """worktree_remove( must not appear as a direct call in worktree_service.py."""
    src = _src(WS_SVC)
    assert "worktree_remove(" not in src, (
        "worktree_remove( is a blocking sync call; wrap with asyncio.to_thread."
    )


def test_get_worktree_status_not_called_directly() -> None:
    """get_worktree_status( must not appear as a direct call in worktree_service.py."""
    src = _src(WS_SVC)
    assert "get_worktree_status(" not in src, (
        "get_worktree_status( is a blocking sync call; wrap with asyncio.to_thread."
    )


# ---------------------------------------------------------------------------
# 5. worktree_service.py handle_dirty_repo: bare _git_run / git_run calls gone
# ---------------------------------------------------------------------------


def test_no_bare_git_run_in_handle_dirty_repo() -> None:
    """git_run must not be called directly in handle_dirty_repo.

    The bare local-alias pattern (_git_run = git_run; _git_run(...)) must be
    replaced with asyncio.to_thread.
    """
    src = _src(WS_SVC)
    # The old pattern imported git_run as _git_run then called it directly.
    assert "_git_run(" not in src, (
        "Bare _git_run( call found; wrap git_run with asyncio.to_thread."
    )


# ---------------------------------------------------------------------------
# 6. workspace/service.py: detect_default_branch not called directly
# ---------------------------------------------------------------------------


def test_detect_default_branch_not_called_directly() -> None:
    """detect_default_branch must not be called bare (= detect_default_branch(...))."""
    src = _src(REPO_SVC)
    # A bare call would be assigned: `default_branch = detect_default_branch(...)`.
    # After fix it must be: `await asyncio.to_thread(detect_default_branch, ...)`.
    assert "= detect_default_branch(" not in src, (
        "detect_default_branch( is blocking; wrap with asyncio.to_thread."
    )


# ---------------------------------------------------------------------------
# 7. tool_handlers.py: ConflictChecker().check() wrapped in to_thread
# ---------------------------------------------------------------------------


def test_conflict_checker_not_called_directly() -> None:
    """ConflictChecker().check( must not be a bare sync call in _handle_check_conflict.

    simulate_merge runs git subprocess; must not block the event loop.
    """
    src = _src(TOOL_H)
    assert "ConflictChecker().check(" not in src, (
        "ConflictChecker().check( is blocking; wrap with asyncio.to_thread."
    )


# ---------------------------------------------------------------------------
# 8. asyncio.to_thread actually appears in each fixed file
# ---------------------------------------------------------------------------


def test_worktree_service_uses_to_thread() -> None:
    """asyncio.to_thread must appear in worktree_service.py after B3."""
    assert "to_thread" in _src(WS_SVC), (
        "asyncio.to_thread not found in worktree_service.py — git calls still sync."
    )


def test_workspace_service_uses_to_thread() -> None:
    """asyncio.to_thread must appear in workspace/service.py after B3."""
    assert "to_thread" in _src(REPO_SVC), (
        "asyncio.to_thread not found in workspace/service.py — git calls still sync."
    )


def test_tool_handlers_uses_to_thread() -> None:
    """asyncio.to_thread must appear in tool_handlers.py after B3."""
    assert "to_thread" in _src(TOOL_H), (
        "asyncio.to_thread not found in tool_handlers.py — ConflictChecker still sync."
    )
