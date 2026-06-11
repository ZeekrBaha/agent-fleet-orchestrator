"""MemoryService — per-agent memory storage for Fleet (AC-041).

Public API:
    MemoryService(db)
    MemoryService.write(agent_id, scope, kind, content, metadata=None) -> int
    MemoryService.read_recent(agent_id, scope, *, kind=None, limit=20)
        -> list[MemoryRecord]
    MemoryService.delete(memory_id) -> None

Writes go through the DatabaseManager write queue (single-writer).
Reads use read_connection() (concurrent reads OK).

Memory writes emit no events — event emission is the caller's responsibility.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from fleet.db import DatabaseManager
from fleet.models import MemoryKind, MemoryRecord


class MemoryService:
    """Stores and retrieves agent memory rows in the `agent_memories` table."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def write(
        self,
        agent_id: str,
        scope: str,
        kind: MemoryKind,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> int:
        """Insert one memory row and return its auto-increment id.

        Args:
            agent_id: The agent that owns this memory.
            scope:    The fleet scope (workspace/project name).
            kind:     Memory kind — e.g. "compaction" or "note".
            content:  The memory text (e.g. compaction summary).
            metadata: Optional key/value metadata dict.

        Returns:
            The integer id of the newly inserted row.
        """
        ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        metadata_json = json.dumps(metadata or {})

        def _write(conn: Connection) -> int:
            result = conn.execute(
                text(
                    "INSERT INTO agent_memories"
                    " (agent_id, scope, kind, content, metadata_json, ts)"
                    " VALUES (:agent_id, :scope, :kind, :content, :metadata_json, :ts)"
                ),
                {
                    "agent_id": agent_id,
                    "scope": scope,
                    "kind": kind,
                    "content": content,
                    "metadata_json": metadata_json,
                    "ts": ts,
                },
            )
            conn.commit()
            last_id = result.lastrowid
            if last_id is None:
                raise RuntimeError("INSERT into agent_memories did not return a rowid")
            return int(last_id)

        return await self._db.write(_write)

    async def read_recent(
        self,
        agent_id: str,
        scope: str,
        *,
        kind: MemoryKind | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        """Return the most-recent memory rows for *agent_id* in *scope*.

        Args:
            agent_id: The agent whose memories to fetch.
            scope:    The fleet scope.
            kind:     Optional filter — only return rows with this kind.
            limit:    Maximum rows to return (default 20), newest-last order.

        Returns:
            List of MemoryRecord ordered by id ASC (oldest first).
        """
        conditions = ["agent_id = :agent_id", "scope = :scope"]
        params: dict[str, object] = {
            "agent_id": agent_id,
            "scope": scope,
            "limit": limit,
        }

        if kind is not None:
            conditions.append("kind = :kind")
            params["kind"] = kind

        where = " AND ".join(conditions)
        sql = text(
            f"SELECT id, agent_id, scope, kind, content, metadata_json, ts"
            f" FROM agent_memories WHERE {where}"
            f" ORDER BY id DESC LIMIT :limit"
        )

        records: list[MemoryRecord] = []
        with self._db.read_connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        # Reverse so result is oldest-first (ascending id order)
        for row in reversed(rows):
            records.append(
                MemoryRecord(
                    id=row.id,
                    agent_id=row.agent_id,
                    scope=row.scope,
                    kind=row.kind,
                    content=row.content,
                    metadata=json.loads(row.metadata_json),
                    ts=row.ts,
                )
            )
        return records

    async def delete(self, memory_id: int) -> None:
        """Delete the memory row with the given id.

        Args:
            memory_id: The integer id of the row to delete.
        """
        def _write(conn: Connection) -> None:
            conn.execute(
                text("DELETE FROM agent_memories WHERE id = :id"),
                {"id": memory_id},
            )
            conn.commit()

        await self._db.write(_write)
