"""WorkspaceService — repository registry for the Fleet orchestrator.

Manages the ``repositories`` table: registering, querying, and unregistering
git repositories.  All writes go through the DatabaseManager's single-writer
queue; reads use read_connection().

Worktree management lives in ``fleet.workspace.worktree_service``; this module
re-exports the key names so callers can import from one place:

    from fleet.workspace.service import (
        WorkspaceService, WorktreeService,
        DirtyRepoError, OverlapError, WorktreeError,
    )

Public API:
    RepositoryRecord                               — dataclass for a repo row
    WorkspaceService(db, event_service)
    WorkspaceService.register_repo(path, ...)      -> RepositoryRecord
    WorkspaceService.get_repo(repo_id)             -> RepositoryRecord | None
    WorkspaceService.get_repo_by_path(path)        -> RepositoryRecord | None
    WorkspaceService.list_repos()                  -> list[RepositoryRecord]
    WorkspaceService.unregister_repo(repo_id)      -> None
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, text

from fleet.db import DatabaseManager
from fleet.events.service import EventService
from fleet.workspace.gitops import GitError, detect_default_branch

# Re-export worktree types so callers can ``from fleet.workspace.service import``
# all workspace-related names from a single module.
from fleet.workspace.worktree_service import (  # noqa: F401
    DirtyRepoError,
    OverlapError,
    WorktreeError,
    WorktreeService,
)


@dataclass
class RepositoryRecord:
    """Row from the ``repositories`` table."""

    id: str
    path: str
    default_branch: str
    merge_policy: dict[str, object]
    created_at: str


def _row_to_record(row: object) -> RepositoryRecord:
    """Convert a SQLAlchemy row to a RepositoryRecord."""
    return RepositoryRecord(
        id=row.id,  # type: ignore[attr-defined]
        path=row.path,  # type: ignore[attr-defined]
        default_branch=row.default_branch,  # type: ignore[attr-defined]
        merge_policy=json.loads(row.merge_policy_json),  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
    )


class WorkspaceService:
    """Registry for git repositories used by the Fleet orchestrator."""

    def __init__(
        self,
        db: DatabaseManager,
        event_service: EventService,
    ) -> None:
        self._db = db
        self._event_service = event_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def register_repo(
        self,
        path: str,
        *,
        default_branch: str | None = None,
        merge_policy: dict[str, object] | None = None,
    ) -> RepositoryRecord:
        """Register a git repository by filesystem path.

        Validates that:
        - The path exists on disk.
        - The path contains a ``.git`` entry (is a git repo).
        - The path is not already registered (UNIQUE constraint).

        If ``default_branch`` is None, it is auto-detected via
        ``detect_default_branch()``.

        Args:
            path:           Absolute path to the repository root.
            default_branch: Override branch detection; auto-detected if None.
            merge_policy:   Optional JSON-serialisable dict; defaults to {}.

        Returns:
            RepositoryRecord for the newly registered repo.

        Raises:
            ValueError: If path doesn't exist, isn't a git repo, or is already
                        registered.
        """
        repo_path = Path(path).resolve()
        path = str(repo_path)  # canonical form stored in DB and used for all checks

        if not repo_path.exists():
            raise ValueError(f"Path does not exist: {path!r}")

        if not (repo_path / ".git").exists():
            raise ValueError(f"Path is not a git repository (no .git): {path!r}")

        # Check for duplicate before hitting the DB constraint to produce a
        # cleaner error message.
        existing = await self.get_repo_by_path(path)
        if existing is not None:
            raise ValueError(f"Repository already registered: {path!r}")

        # Auto-detect branch if not supplied.
        if default_branch is None:
            try:
                default_branch = detect_default_branch(repo_path)
            except GitError as exc:
                # Can't detect branch — fall back to "main" here.
                # This is the single fallback point for default branch detection.
                # The repo is still valid; using a sensible default is acceptable.
                _ = exc  # suppress; we'll use the fallback
                default_branch = "main"

        repo_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        policy_json = json.dumps(merge_policy or {})

        def _write(conn: Connection) -> None:
            conn.execute(
                text(
                    "INSERT INTO repositories"
                    " (id, path, default_branch, merge_policy_json, created_at)"
                    " VALUES"
                    " (:id, :path, :default_branch, :merge_policy_json, :created_at)"
                ),
                {
                    "id": repo_id,
                    "path": path,
                    "default_branch": default_branch,
                    "merge_policy_json": policy_json,
                    "created_at": now,
                },
            )
            conn.commit()

        await self._db.write(_write)

        await self._event_service.append(
            "workspace",
            "state_change",
            f"Repository registered: {path}",
            payload={"repo_id": repo_id, "path": path, "action": "registered"},
        )

        record = await self.get_repo(repo_id)
        assert record is not None, "just-inserted repo should be retrievable"
        return record

    async def get_repo(self, repo_id: str) -> RepositoryRecord | None:
        """Fetch a repository by its ID.

        Returns:
            RepositoryRecord if found, None otherwise.
        """
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT id, path, default_branch, merge_policy_json, created_at"
                    " FROM repositories WHERE id = :id"
                ),
                {"id": repo_id},
            ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def get_repo_by_path(self, path: str) -> RepositoryRecord | None:
        """Fetch a repository by its filesystem path.

        Returns:
            RepositoryRecord if found, None otherwise.
        """
        canonical = str(Path(path).resolve())
        with self._db.read_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT id, path, default_branch, merge_policy_json, created_at"
                    " FROM repositories WHERE path = :path"
                ),
                {"path": canonical},
            ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def list_repos(self) -> list[RepositoryRecord]:
        """Return all registered repositories, ordered by creation time.

        Returns:
            List of RepositoryRecord (may be empty).
        """
        with self._db.read_connection() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, path, default_branch, merge_policy_json, created_at"
                    " FROM repositories ORDER BY created_at ASC"
                )
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    async def unregister_repo(self, repo_id: str) -> None:
        """Remove a repository from the registry.

        Does NOT delete any files on disk — only the DB row is removed.
        Emits a state_change event after deletion.

        Args:
            repo_id: ID of the repository to remove.
        """
        # Fetch path before delete for the event payload.
        record = await self.get_repo(repo_id)

        def _write(conn: Connection) -> None:
            conn.execute(
                text("DELETE FROM repositories WHERE id = :id"),
                {"id": repo_id},
            )
            conn.commit()

        await self._db.write(_write)

        path = record.path if record is not None else ""
        await self._event_service.append(
            "workspace",
            "state_change",
            f"Repository unregistered: {path}",
            payload={"repo_id": repo_id, "path": path, "action": "unregistered"},
        )
