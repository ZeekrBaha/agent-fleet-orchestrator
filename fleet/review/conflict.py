"""ConflictChecker — dry-run merge conflict detection for Fleet.

Public API:
    ConflictResult          — Pydantic model: has_conflict, conflict_files, summary
    ConflictChecker         — wraps simulate_merge(); parses conflict filenames
    ConflictChecker.check(repo_path, branch, target_branch) -> ConflictResult
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from fleet.workspace.gitops import simulate_merge


class ConflictResult(BaseModel):
    """Result of a dry-run merge conflict check."""

    has_conflict: bool
    conflict_files: list[str]
    summary: str


_FILENAME_PATTERN = re.compile(r"^[+\-<>=!]+\s+(.+)$")


def _extract_conflict_files(summary: str) -> list[str]:
    """Parse conflict filenames from git merge-tree output.

    git merge-tree output format for conflicts typically contains lines like:
      ``<<<<<<< .our`` or ``CONFLICT (content): Merge conflict in <filename>``
    We look for the ``CONFLICT … in <filename>`` pattern first, then fall back
    to a heuristic on marker lines.
    """
    files: list[str] = []
    seen: set[str] = set()

    for line in summary.splitlines():
        # "CONFLICT (content): Merge conflict in path/to/file"
        match = re.search(r"CONFLICT[^:]*:\s+.*\bin\s+(\S+)", line)
        if match:
            fname = match.group(1).strip()
            if fname and fname not in seen:
                seen.add(fname)
                files.append(fname)

    return files


class ConflictChecker:
    """Wraps simulate_merge() to produce a ConflictResult."""

    def check(
        self,
        repo_path: str | Path,
        branch: str,
        target_branch: str,
    ) -> ConflictResult:
        """Check whether merging *branch* into *target_branch* would conflict.

        The repository is NEVER modified (simulate_merge uses git merge-tree).

        Args:
            repo_path:     Path to the repository root (main worktree).
            branch:        Source branch to be merged.
            target_branch: Destination branch.

        Returns:
            ConflictResult with has_conflict, conflict_files, summary.
        """
        has_conflict, summary = simulate_merge(repo_path, branch, target_branch)
        conflict_files = _extract_conflict_files(summary) if has_conflict else []
        return ConflictResult(
            has_conflict=has_conflict,
            conflict_files=conflict_files,
            summary=summary,
        )
