"""EventService — append events to the DB and fan-out via SSEHub.

Public API:
    EventService.append(scope, type, summary, *, agent_id, payload) -> int
    EventService.query(scope, *, agent_id, type_filter, after_id, limit) -> list[Event]
    create_event_service(db, hub) -> EventService
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import Connection, text

from fleet.db import DatabaseManager
from fleet.models import Event
from fleet.util.time import utcnow_iso

if TYPE_CHECKING:
    from fleet.events.sse import SSEHub


class EventService:
    """Writes events to the DB and publishes them to the SSEHub."""

    def __init__(self, db: DatabaseManager, hub: SSEHub) -> None:
        self._db = db
        self._hub = hub

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def append(
        self,
        scope: str,
        type: str,  # noqa: A002 — shadowing built-in intentional per spec
        summary: str,
        *,
        agent_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> int:
        """Write one event to the DB; return its auto-increment id.

        Publishes to SSEHub after the commit so subscribers always see
        already-committed data.
        """
        ts = utcnow_iso()
        payload_json = json.dumps(payload or {})

        def _write(conn: Connection) -> int:
            sql = text(
                "INSERT INTO events"
                " (ts, scope, agent_id, type, summary, payload_json)"
                " VALUES (:ts, :scope, :agent_id, :type, :summary, :payload_json)"
            )
            result = conn.execute(
                sql,
                {
                    "ts": ts,
                    "scope": scope,
                    "agent_id": agent_id,
                    "type": type,
                    "summary": summary,
                    "payload_json": payload_json,
                },
            )
            conn.commit()
            last_id = result.lastrowid
            if last_id is None:
                raise RuntimeError("INSERT did not return a rowid")
            return int(last_id)

        event_id: int = await self._db.write(_write)

        event = Event(
            id=event_id,
            ts=ts,
            scope=scope,
            agent_id=agent_id,
            type=type,
            summary=summary,
            payload=payload or {},
        )
        await self._hub.publish(scope, event)
        return event_id

    async def query(
        self,
        scope: str,
        *,
        agent_id: str | None = None,
        type_filter: str | None = None,
        after_id: int | None = None,
        limit: int = 200,
    ) -> list[Event]:
        """Read events from the DB; return them newest-last (ascending id order).

        Filtering:
          - scope: always applied (equality)
          - agent_id: equality filter, if provided
          - type_filter: equality filter on the ``type`` column, if provided
          - after_id: only events with id > after_id, if provided
          - limit: max rows returned (default 200)
        """
        conditions = ["scope = :scope"]
        params: dict[str, object] = {"scope": scope, "limit": limit}

        if agent_id is not None:
            conditions.append("agent_id = :agent_id")
            params["agent_id"] = agent_id

        if type_filter is not None:
            conditions.append("type = :type_filter")
            params["type_filter"] = type_filter

        if after_id is not None:
            conditions.append("id > :after_id")
            params["after_id"] = after_id

        where = " AND ".join(conditions)
        sql = text(
            f"SELECT id, ts, scope, agent_id, type, summary, payload_json"
            f" FROM events WHERE {where} ORDER BY id ASC LIMIT :limit"
        )

        events: list[Event] = []
        with self._db.read_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                events.append(
                    Event(
                        id=row.id,
                        ts=row.ts,
                        scope=row.scope,
                        agent_id=row.agent_id,
                        type=row.type,
                        summary=row.summary,
                        payload=json.loads(row.payload_json),
                    )
                )
        return events


def create_event_service(db: DatabaseManager, hub: SSEHub) -> EventService:
    """Factory: wire a DatabaseManager and SSEHub into an EventService."""
    return EventService(db, hub)
