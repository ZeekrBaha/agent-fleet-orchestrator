"""B4: SSE robustness — catch-up pagination, QueueFull close, client fix.

TDD RED phase.

Behaviors tested:
1. Catch-up with >200 backlog events delivers ALL events (pagination).
2. QueueFull closes the subscription (iterator terminates), not silent drop.
3. app.js buildStreamUrl does not append after_id query param.
"""
from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from fleet.db import init_db
from fleet.events.service import EventService

APP_JS = Path(__file__).parent.parent / "fleet" / "static" / "app.js"

# ---------------------------------------------------------------------------
# Shared live-server helpers (copied from test_events.py pattern)
# ---------------------------------------------------------------------------


async def _run_server(
    app: FastAPI, host: str, port: int, started: asyncio.Event
) -> None:
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="error")
    server = uvicorn.Server(config)
    original_startup = server.startup

    async def _startup(sockets: object = None) -> None:
        await original_startup(sockets)
        started.set()

    server.startup = _startup  # type: ignore[method-assign]
    await server.serve()


@asynccontextmanager
async def _live_server(app: FastAPI, port: int) -> AsyncIterator[str]:
    started = asyncio.Event()
    task = asyncio.create_task(_run_server(app, "127.0.0.1", port, started))
    await asyncio.wait_for(started.wait(), timeout=10.0)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _build_sse_app(db_path: str) -> FastAPI:
    from collections.abc import AsyncIterator as _AI
    from contextlib import asynccontextmanager

    from fleet.api.auth import require_token
    from fleet.api.events import router
    from fleet.events.service import create_event_service
    from fleet.events.sse import SSEHub

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> _AI[None]:
        hub = SSEHub()
        manager = await init_db(db_path)
        svc = create_event_service(manager, hub)
        application.state.event_service = svc
        application.state.sse_hub = hub
        yield
        await manager.close()

    async def _no_auth() -> None:
        return None

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.dependency_overrides[require_token] = _no_auth
    return app


# ---------------------------------------------------------------------------
# 1. Catch-up pagination: 250 backlog events → all 250 delivered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catchup_delivers_all_250_events(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Stream with Last-Event-ID: 0 must deliver all 250 missed events.

    Fails before B4 because the single service.query(limit=200) call silently
    caps at 200 — events 201-250 are never delivered.
    """
    db_path = str(tmp_path / "b4_pagination.db")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    app = _build_sse_app(db_path)
    collected: list[dict] = []

    async with _live_server(app, port) as base_url:
        svc: EventService = app.state.event_service  # type: ignore[assignment]

        # Seed 250 events before connecting
        for i in range(250):
            await svc.append("pag-scope", "t", f"event-{i}")

        async with AsyncClient(base_url=base_url, timeout=30.0) as client:

            async def read_catchup() -> None:
                async with client.stream(
                    "GET",
                    "/api/events/stream",
                    params={"scope": "pag-scope"},
                    headers={"Last-Event-ID": "0"},
                ) as resp:
                    assert resp.status_code == 200
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data = json.loads(line[len("data:"):].strip())
                            collected.append(data)
                            if len(collected) >= 250:
                                break

            await asyncio.wait_for(read_catchup(), timeout=20.0)

    assert len(collected) == 250, (
        f"Expected 250 catch-up events, got {len(collected)}. "
        "Catch-up must paginate past the 200-event limit."
    )
    assert collected[0]["summary"] == "event-0"
    assert collected[249]["summary"] == "event-249"


# ---------------------------------------------------------------------------
# 2. QueueFull closes subscription (not silent drop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_full_closes_subscription() -> None:
    """When the subscription queue overflows, the subscription must close.

    Before B4: overflow events are silently dropped; iterator never terminates.
    After B4: overflow triggers subscription close; iterator terminates cleanly.
    """
    from fleet.events.sse import SSEHub
    from fleet.models import Event

    hub = SSEHub()
    sub = hub.subscribe("overflow-scope")

    # Fill the queue to max capacity (maxsize=100)
    for i in range(100):
        ev_i = Event(
            id=i + 1, ts="2026-01-01T00:00:00+00:00", scope="overflow-scope",
            type="t", summary=f"fill-{i}", payload={},
        )
        sub._put_nowait(ev_i)

    # Overflow: one more event when queue is full
    overflow_ev = Event(
        id=101, ts="2026-01-01T00:00:00+00:00", scope="overflow-scope",
        type="t", summary="overflow", payload={},
    )
    sub._put_nowait(overflow_ev)

    # Drain: if QueueFull closes the subscription the iterator terminates.
    # If QueueFull silently drops, the iterator hangs waiting for more events.
    drained = False

    async def _drain() -> None:
        nonlocal drained
        async for _ in sub:
            pass
        drained = True

    try:
        await asyncio.wait_for(_drain(), timeout=1.0)
    except TimeoutError:
        pass  # timeout = iterator is still blocked = silent drop (failure)

    assert drained, (
        "Subscription iterator did not terminate after queue overflow. "
        "QueueFull must close the subscription, not silently drop events."
    )


# ---------------------------------------------------------------------------
# 3. app.js: buildStreamUrl must not append after_id query param
# ---------------------------------------------------------------------------


def test_app_js_no_after_id_query_param() -> None:
    """buildStreamUrl in app.js must not append after_id to the URL.

    The server reads Last-Event-ID header (sent automatically by browser
    EventSource on reconnect), not after_id query param. Passing after_id
    in the URL means catch-up never fires on reconnect.
    """
    src = APP_JS.read_text(encoding="utf-8")
    assert "after_id" not in src, (
        "app.js still appends after_id query param. "
        "Remove it — the browser sends Last-Event-ID header automatically."
    )
