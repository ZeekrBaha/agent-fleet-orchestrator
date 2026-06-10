"""GitRepoFactory — creates throwaway git repos for testing.

Used by workspace and worktree tests.  All repos are created under a base
directory (typically ``tmp_path`` from pytest) and cleaned up via
``factory.cleanup()``.

Per-repo git identity is set via local config (not global) to avoid polluting
the developer's git identity.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _git(args: list[str], *, cwd: Path) -> None:
    """Run a git command, raising on non-zero exit.

    Uses check=True so CalledProcessError surfaces test failures clearly.
    """
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _configure_identity(path: Path) -> None:
    """Set per-repo git identity to avoid 'no identity' commit errors."""
    _git(["config", "user.email", "test@fleet.local"], cwd=path)
    _git(["config", "user.name", "Fleet Test"], cwd=path)


class GitRepoFactory:
    """Creates throwaway git repos for testing."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir
        self._repos: list[Path] = []

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    def make_clean(self, name: str = "repo") -> Path:
        """Create a clean git repo with one initial commit.

        Returns:
            Path to the new repo directory.
        """
        path = self._base / name
        path.mkdir(parents=True, exist_ok=True)

        _git(["init", "-b", "main"], cwd=path)
        _configure_identity(path)

        # Create a file and initial commit so HEAD is valid.
        readme = path / "README.md"
        readme.write_text("# Fleet test repo\n")
        _git(["add", "README.md"], cwd=path)
        _git(["commit", "-m", "initial commit"], cwd=path)

        self._repos.append(path)
        return path

    def make_dirty(self, name: str = "dirty") -> Path:
        """Create a repo with an unstaged modification (is_repo_dirty → True).

        Starts as a clean repo, then modifies a tracked file without staging.

        Returns:
            Path to the new repo directory.
        """
        path = self.make_clean(name)

        # Modify a tracked file without staging — makes the tree dirty.
        readme = path / "README.md"
        readme.write_text("# Fleet test repo\n\nunstaged change\n")

        return path

    def make_ahead(self, name: str = "ahead", commits: int = 2) -> Path:
        """Create a repo with N extra commits beyond the initial.

        Useful for simulating an ahead-of-base-branch state.

        Args:
            name:    Directory name under base_dir.
            commits: Number of extra commits to add (default 2).

        Returns:
            Path to the new repo directory.
        """
        path = self.make_clean(name)

        for i in range(commits):
            extra_file = path / f"extra_{i}.txt"
            extra_file.write_text(f"extra commit {i}\n")
            _git(["add", extra_file.name], cwd=path)
            _git(["commit", "-m", f"extra commit {i}"], cwd=path)

        return path

    def make_with_conflict_potential(self, name: str = "conflict") -> Path:
        """Create a repo with two diverged branches that both modify the same file.

        Branch ``main`` and branch ``feature`` each have a commit that modifies
        ``file.txt`` to different content.  Merging ``feature`` into ``main``
        will produce a conflict.

        Returns:
            Path to the new repo directory.
        """
        path = self.make_clean(name)

        # Create shared file on main.
        shared = path / "file.txt"
        shared.write_text("original content\n")
        _git(["add", "file.txt"], cwd=path)
        _git(["commit", "-m", "add shared file"], cwd=path)

        # Create and switch to feature branch; modify the shared file.
        _git(["checkout", "-b", "feature"], cwd=path)
        shared.write_text("feature branch change\n")
        _git(["add", "file.txt"], cwd=path)
        _git(["commit", "-m", "feature: modify shared file"], cwd=path)

        # Switch back to main; make a conflicting change.
        _git(["checkout", "main"], cwd=path)
        shared.write_text("main branch change\n")
        _git(["add", "file.txt"], cwd=path)
        _git(["commit", "-m", "main: modify shared file"], cwd=path)

        return path

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove all repos created by this factory instance."""
        for repo in self._repos:
            if repo.exists():
                shutil.rmtree(repo, ignore_errors=True)
        self._repos.clear()
