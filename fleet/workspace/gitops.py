"""Pure git subprocess helpers for Fleet workspace operations.

All functions take a ``cwd`` path (the repository root) and run git with a
configurable timeout.  No business logic lives here — only subprocess wrappers.

Public API:
    GitError                          — raised on non-zero git exit or timeout
    git_run(cmd, cwd, timeout)        — run git, return stdout
    detect_default_branch(repo_path)  — infer default branch name
    is_repo_dirty(repo_path)          — True if working tree has uncommitted changes
    list_untracked(repo_path)         — list of untracked/modified files
    worktree_add(repo, wt, branch, base_branch) — create a new git worktree
    worktree_remove(repo, wt)         — remove a git worktree + prune
    get_worktree_status(wt)           — ahead/dirty_files/branch dict
    simulate_merge(repo, branch, target) — dry-run merge, returns (conflict, summary)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GitError(Exception):
    """Raised when a git subprocess exits non-zero or times out."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"git {' '.join(cmd[1:])} failed (exit {returncode}): {stderr.strip()}"
        )


def git_run(
    cmd: list[str],
    *,
    cwd: str | Path,
    timeout: int = 30,
) -> str:
    """Run a git command and return stripped stdout.

    Args:
        cmd:     Full command list, e.g. ``["git", "status", "--porcelain"]``.
        cwd:     Working directory (repository root).
        timeout: Seconds before the subprocess is killed.

    Returns:
        Decoded stdout, stripped of leading/trailing whitespace.

    Raises:
        GitError: on non-zero exit code or subprocess.TimeoutExpired.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raw = exc.stderr or b""
        stderr = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        raise GitError(cmd, -1, f"timed out after {timeout}s: {stderr}") from exc

    if result.returncode != 0:
        raise GitError(cmd, result.returncode, result.stderr)

    return result.stdout.strip()


def detect_default_branch(repo_path: str | Path) -> str:
    """Return the default branch name for the repository.

    Strategy:
    1. Try ``git symbolic-ref --short HEAD`` — works when HEAD is attached.
    2. Fall back to checking whether ``main`` or ``master`` refs exist.
    3. Raise GitError if no branch can be detected.

    Args:
        repo_path: Path to the repository root.

    Returns:
        Branch name string.

    Raises:
        GitError: If no detectable branch is found. Caller may use a fallback.
    """
    # Try the symbolic ref first (the normal case for a freshly cloned repo).
    try:
        branch = git_run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo_path,
        )
        if branch:
            return branch
    except GitError:
        pass  # detached HEAD or bare repo — fall through to probe

    # Probe common branch names via show-ref.
    for candidate in ("main", "master", "trunk", "development"):
        try:
            git_run(
                ["git", "show-ref", "--verify", f"refs/heads/{candidate}"],
                cwd=repo_path,
            )
            return candidate
        except GitError:
            continue

    # No detectable branch — raise error. Caller must provide fallback if needed.
    raise GitError(cmd=[], returncode=1, stderr="cannot determine default branch")


def is_repo_dirty(repo_path: str | Path) -> bool:
    """Return True if the working tree has uncommitted changes.

    Uses ``git status --porcelain`` — any output means dirty.

    Args:
        repo_path: Path to the repository root.
    """
    output = git_run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
    )
    return bool(output)


def list_untracked(repo_path: str | Path) -> list[str]:
    """Return a list of untracked and modified files.

    Uses ``git status --porcelain``, parses the two-character status prefix,
    and returns the file paths (relative to repo root).

    Args:
        repo_path: Path to the repository root.
    """
    output = git_run(
        ["git", "status", "--porcelain"],
        cwd=repo_path,
    )
    if not output:
        return []

    files: list[str] = []
    for line in output.splitlines():
        # porcelain format: "XY filename" — first two chars are status flags
        if len(line) > 3:
            files.append(line[3:].strip())
    return files


# ---------------------------------------------------------------------------
# Worktree helpers (Task 3.2)
# ---------------------------------------------------------------------------


def worktree_add(
    repo_path: str | Path,
    worktree_path: str | Path,
    branch: str,
    *,
    base_branch: str | None = None,
) -> None:
    """Create a new worktree at *worktree_path* on *branch*.

    If *branch* doesn't already exist, it is created from *base_branch*
    (or HEAD if *base_branch* is None).

    On any failure the worktree directory is removed to avoid orphans.

    Args:
        repo_path:     Path to the main repository root.
        worktree_path: Destination directory for the new worktree (must not exist).
        branch:        Branch name to check out in the new worktree.
        base_branch:   Existing branch/commit to branch from; defaults to HEAD.

    Raises:
        GitError: If git worktree add fails.  worktree_path is cleaned up first.
    """
    wt_path = Path(worktree_path)
    repo = Path(repo_path)

    # Determine whether the branch already exists.
    branch_exists = False
    try:
        git_run(
            ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
            cwd=repo,
        )
        branch_exists = True
    except GitError:
        pass  # branch does not yet exist — we will create it

    try:
        if branch_exists:
            # Check out the existing branch in the new worktree.
            git_run(
                ["git", "worktree", "add", str(wt_path), branch],
                cwd=repo,
            )
        else:
            # Create a new branch and check it out.
            cmd = ["git", "worktree", "add", "-b", branch, str(wt_path)]
            if base_branch is not None:
                cmd.append(base_branch)
            git_run(cmd, cwd=repo)
    except GitError:
        # Cleanup: remove the (potentially partially created) worktree dir.
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
        raise


def worktree_remove(repo_path: str | Path, worktree_path: str | Path) -> None:
    """Remove a worktree and prune stale worktree metadata.

    Runs ``git worktree remove --force <path>`` then ``git worktree prune``.

    Args:
        repo_path:     Path to the main repository root.
        worktree_path: Path to the worktree to remove.

    Raises:
        GitError: If git worktree remove fails.
    """
    repo = Path(repo_path)
    git_run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=repo,
    )
    # Prune stale metadata; failure is non-fatal (best effort).
    try:
        git_run(["git", "worktree", "prune"], cwd=repo)
    except GitError:
        pass  # prune failure is cosmetic; worktree removal already succeeded


def get_worktree_status(worktree_path: str | Path) -> dict[str, object]:
    """Return status information for a worktree.

    Returns a dict with:
        ahead        (int)       — commits ahead of the first parent of HEAD
        dirty_files  (list[str]) — files with uncommitted changes
        branch       (str)       — current branch name (or commit hash if detached)

    Args:
        worktree_path: Path to the worktree directory.
    """
    wt = Path(worktree_path)

    # Current branch name.
    try:
        branch = git_run(["git", "symbolic-ref", "--short", "HEAD"], cwd=wt)
    except GitError:
        # Detached HEAD — return the short commit hash instead.
        branch = git_run(["git", "rev-parse", "--short", "HEAD"], cwd=wt)

    # Commits ahead of the merge-base with the upstream/parent branch.
    # We use HEAD~1 as the "base" if no upstream is configured; if there's only
    # one commit (no parent) we report 0 rather than crashing.
    try:
        ahead_str = git_run(
            ["git", "rev-list", "--count", "HEAD@{upstream}..HEAD"],
            cwd=wt,
        )
        ahead = int(ahead_str) if ahead_str else 0
    except GitError:
        # No upstream configured — count commits since the branch forked from
        # the first parent chain, or fall back to 0.
        try:
            ahead_str = git_run(
                ["git", "rev-list", "--count", "HEAD^..HEAD"],
                cwd=wt,
            )
            ahead = int(ahead_str) if ahead_str else 0
        except GitError:
            # Initial commit with no parent — 0 ahead.
            ahead = 0

    # Uncommitted files (staged + unstaged).
    dirty_output = git_run(["git", "status", "--porcelain"], cwd=wt)
    dirty_files: list[str] = []
    if dirty_output:
        for line in dirty_output.splitlines():
            if len(line) > 3:
                dirty_files.append(line[3:].strip())

    return {"ahead": ahead, "dirty_files": dirty_files, "branch": branch}


def simulate_merge(
    repo_path: str | Path,
    branch: str,
    target_branch: str,
) -> tuple[bool, str]:
    """Simulate a merge of *branch* into *target_branch* without touching the repo.

    Uses ``git merge-tree`` (3-way merge simulation) to detect conflicts.
    The repository is NEVER modified.

    Args:
        repo_path:     Path to the main repository root.
        branch:        The branch to be merged (source).
        target_branch: The branch to merge into (destination).

    Returns:
        (has_conflict, conflict_summary) tuple.
        has_conflict is True if git reports conflict markers.
        conflict_summary is a short human-readable description.
    """
    repo = Path(repo_path)

    # Find the common merge-base commit.
    try:
        merge_base = git_run(
            ["git", "merge-base", target_branch, branch],
            cwd=repo,
        )
    except GitError as exc:
        return True, f"could not find merge-base: {exc}"

    # Run git merge-tree (3-way virtual merge) — does NOT touch the working tree.
    # Uses the deprecated 3-arg form (git < 2.38 compatible).
    # Output contains conflict markers "<<<<<<" when conflicts exist.
    # On git >= 2.38, consider switching to: git merge-tree --stdin
    try:
        output = git_run(
            ["git", "merge-tree", merge_base, target_branch, branch],
            cwd=repo,
        )
    except GitError as exc:
        return True, f"merge-tree failed: {exc}"

    # git merge-tree emits conflict markers when conflicts are present.
    has_conflict = "<<<<<<" in output or "CONFLICT" in output
    if has_conflict:
        # Summarise: first few lines that contain conflict markers.
        conflict_lines = [
            line for line in output.splitlines()
            if any(tok in line for tok in ("<<<<<<", ">>>>>>", "=======", "CONFLICT"))
        ]
        summary = "\n".join(conflict_lines[:10])
    else:
        summary = ""

    return has_conflict, summary
