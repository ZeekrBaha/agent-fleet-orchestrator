"""FastAPI router for the Fleet event log.

Endpoints:
    POST /api/events          — append an event
    GET  /api/events          — query events
    GET  /api/events/stream   — SSE live stream (with optional catch-up)
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from fleet.api.auth import require_token
from fleet.events.service import EventService
from fleet.events.sse import SSEHub, Subscription
from fleet.models import Event

router = APIRouter(prefix="/api/events", tags=["events"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class EventCreate(BaseModel):
    scope: str
    type: str
    summary: str
    agent_id: str | None = None
    payload: dict[str, object] = {}


class EventCreated(BaseModel):
    id: int


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_service(request: Request) -> EventService:
    service: EventService = request.app.state.event_service
    return service


def _get_hub(request: Request) -> SSEHub:
    hub: SSEHub = request.app.state.sse_hub
    return hub


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=EventCreated)
async def append_event(
    body: EventCreate,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[EventService, Depends(_get_service)],
) -> EventCreated:
    """Append a new event and return its id."""
    event_id = await service.append(
        body.scope,
        body.type,
        body.summary,
        agent_id=body.agent_id,
        payload=body.payload,
    )
    return EventCreated(id=event_id)


@router.get("", response_model=list[Event])
async def query_events(
    scope: str,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[EventService, Depends(_get_service)],
    agent_id: str | None = None,
    type_filter: str | None = None,
    after_id: int | None = None,
    limit: int = 200,
) -> list[Event]:
    """Query events with optional filters."""
    return await service.query(
        scope,
        agent_id=agent_id,
        type_filter=type_filter,
        after_id=after_id,
        limit=limit,
    )


@router.get("/stream")
async def stream_events(
    scope: str,
    request: Request,
    _auth: Annotated[None, Depends(require_token)],
    service: Annotated[EventService, Depends(_get_service)],
    hub: Annotated[SSEHub, Depends(_get_hub)],
) -> EventSourceResponse:
    """SSE stream for a scope.

    If the client sends ``Last-Event-ID``, all events with id greater than
    that value are replayed first (catch-up), then live events follow.
    """
    last_event_id_raw = request.headers.get("Last-Event-ID")
    after_id: int | None = None
    if last_event_id_raw is not None:
        try:
            after_id = int(last_event_id_raw)
        except ValueError:
            # Non-integer Last-Event-ID is ignored; stream from live only.
            after_id = None

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        sub: Subscription | None = None
        try:
            # Subscribe *before* querying catch-up events to avoid a race
            # where a new event arrives between the query and subscribe.
            sub = hub.subscribe(scope)

            # Catch-up: replay any events the client missed.
            if after_id is not None:
                catchup_events = await service.query(scope, after_id=after_id)
                for ev in catchup_events:
                    if await request.is_disconnected():
                        return
                    yield {
                        "data": json.dumps(ev.model_dump()),
                        "id": str(ev.id),
                    }

            # Live: stream new events as they arrive.
            async for ev in sub:
                if await request.is_disconnected():
                    return
                yield {
                    "data": json.dumps(ev.model_dump()),
                    "id": str(ev.id),
                }
        except asyncio.CancelledError:
            # Client disconnected — clean up without re-raising as a traceback.
            return
        finally:
            if sub is not None:
                hub.unsubscribe(scope, sub)

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Type alias for generator — needed to satisfy mypy in the closure above
# ---------------------------------------------------------------------------

from collections.abc import AsyncIterator  # noqa: E402 — after router definition
