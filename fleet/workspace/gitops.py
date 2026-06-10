"""Pure git subprocess helpers for Fleet workspace operations.

All functions take a ``cwd`` path (the repository root) and run git with a
configurable timeout.  No business logic lives here — only subprocess wrappers.

Public API:
    GitError                        — raised on non-zero git exit or timeout
    git_run(cmd, cwd, timeout)      — run git, return stdout
    detect_default_branch(repo_path) — infer default branch name
    is_repo_dirty(repo_path)        — True if working tree has uncommitted changes
    list_untracked(repo_path)       — list of untracked/modified files
"""

from __future__ import annotations

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
