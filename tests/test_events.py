"""Tests for Event Log Service + SSE Hub (Task 1.3).

TDD: tests written FIRST; implementation follows.

Run:  uv run pytest tests/test_events.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fleet.db import DatabaseManager, init_db
from fleet.events.service import EventService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(tmp_path: pytest.TempPathFactory) -> AsyncIterator[DatabaseManager]:
    db_path = str(tmp_path / "test_events.db")
    manager = await init_db(db_path)
    yield manager
    await manager.close()


@pytest_asyncio.fixture
async def hub() -> AsyncIterator[SSEHub]:  # noqa: F821 — imported lazily
    from fleet.events.sse import SSEHub

    yield SSEHub()


@pytest_asyncio.fixture
async def service(db: DatabaseManager, hub: SSEHub) -> AsyncIterator[EventService]:  # noqa: F821
    from fleet.events.service import create_event_service

    yield create_event_service(db, hub)


@pytest_asyncio.fixture
async def app_client(tmp_path: pytest.TempPathFactory) -> AsyncIterator[AsyncClient]:
    """Wire up a real FastAPI app with all dependencies injected."""
    from fleet.api.events import router
    from fleet.events.service import create_event_service
    from fleet.events.sse import SSEHub

    db_path = str(tmp_path / "app_test.db")
    manager = await init_db(db_path)
    sse_hub = SSEHub()
    svc = create_event_service(manager, sse_hub)

    app = FastAPI()
    app.include_router(router)

    # Override the dependency so no real token is needed for most tests
    from fleet.api.auth import require_token

    async def _no_auth() -> None:
        return None

    app.dependency_overrides[require_token] = _no_auth
    # Inject service + hub into app state so router can access them
    app.state.event_service = svc
    app.state.sse_hub = sse_hub

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client

    await manager.close()


# ---------------------------------------------------------------------------
# Unit tests — EventService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_returns_incrementing_ids(
    service: EventService,  # noqa: F821
) -> None:
    """append() for 3 events returns strictly increasing IDs."""
    id1 = await service.append("scope-a", "task.created", "first event")
    id2 = await service.append("scope-a", "task.created", "second event")
    id3 = await service.append("scope-a", "task.created", "third event")

    assert isinstance(id1, int) and id1 > 0
    assert id2 > id1
    assert id3 > id2


@pytest.mark.asyncio
async def test_query_filter_by_scope(
    service: EventService,  # noqa: F821
) -> None:
    """query() returns only events for the requested scope."""
    await service.append("scope-x", "evt.type", "in scope-x")
    await service.append("scope-y", "evt.type", "in scope-y")
    await service.append("scope-x", "evt.type", "also in scope-x")

    results = await service.query("scope-x")
    assert len(results) == 2
    assert all(e.scope == "scope-x" for e in results)


@pytest.mark.asyncio
async def test_query_filter_by_agent_id(
    service: EventService,  # noqa: F821
) -> None:
    """query(agent_id=...) filters by agent."""
    await service.append("s", "t", "no agent")
    await service.append("s", "t", "agent alpha", agent_id="alpha")
    await service.append("s", "t", "agent beta", agent_id="beta")

    results = await service.query("s", agent_id="alpha")
    assert len(results) == 1
    assert results[0].agent_id == "alpha"


@pytest.mark.asyncio
async def test_query_filter_by_type(
    service: EventService,  # noqa: F821
) -> None:
    """query(type_filter=...) returns only events of that type."""
    await service.append("s", "task.created", "created")
    await service.append("s", "task.updated", "updated")
    await service.append("s", "task.created", "created again")

    results = await service.query("s", type_filter="task.created")
    assert len(results) == 2
    assert all(e.type == "task.created" for e in results)


@pytest.mark.asyncio
async def test_query_after_id(
    service: EventService,  # noqa: F821
) -> None:
    """query(after_id=N) returns only events with id > N, newest last."""
    ids = []
    for i in range(5):
        eid = await service.append("s", "t", f"event {i}")
        ids.append(eid)

    # after_id = ids[1] means we want events 3, 4, 5 (indices 2,3,4)
    results = await service.query("s", after_id=ids[1])
    assert len(results) == 3
    result_ids = [e.id for e in results]
    assert result_ids == sorted(result_ids)  # newest last = ascending id order
    assert all(eid is not None and eid > ids[1] for eid in result_ids)


@pytest.mark.asyncio
async def test_append_publishes_to_hub(
    service: EventService,  # noqa: F821
    hub: SSEHub,  # noqa: F821
) -> None:
    """After append(), a hub subscriber receives the event."""
    sub = hub.subscribe("pub-scope")

    eid = await service.append("pub-scope", "task.done", "published")

    # Collect the published event with a short timeout
    received: list[object] = []

    async def collect() -> None:
        async for event in sub:
            received.append(event)
            break  # stop after first event

    try:
        await asyncio.wait_for(collect(), timeout=2.0)
    finally:
        hub.unsubscribe("pub-scope", sub)

    assert len(received) == 1
    from fleet.models import Event

    ev = received[0]
    assert isinstance(ev, Event)
    assert ev.id == eid
    assert ev.summary == "published"


# ---------------------------------------------------------------------------
# Unit tests — SSEHub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hub_fan_out_to_multiple_subscribers(hub: SSEHub) -> None:  # noqa: F821
    """publish() delivers event to all subscribers for the same scope."""
    from fleet.events.sse import SSEHub
    from fleet.models import Event

    h = SSEHub()
    sub1 = h.subscribe("fan")
    sub2 = h.subscribe("fan")

    ev = Event(
        id=1, ts="2026-01-01T00:00:00Z", scope="fan",
        type="t", summary="hi", payload={},
    )
    await h.publish("fan", ev)

    received1: list[Event] = []
    received2: list[Event] = []

    async def drain(sub, out: list[Event]) -> None:
        async for e in sub:
            out.append(e)
            break

    await asyncio.gather(
        asyncio.wait_for(drain(sub1, received1), 2.0),
        asyncio.wait_for(drain(sub2, received2), 2.0),
    )
    h.unsubscribe("fan", sub1)
    h.unsubscribe("fan", sub2)

    assert len(received1) == 1
    assert len(received2) == 1
    assert received1[0].summary == "hi"


@pytest.mark.asyncio
async def test_hub_scope_isolation(hub: SSEHub) -> None:  # noqa: F821
    """publish() to scope A does not deliver to scope B subscribers."""
    from fleet.events.sse import SSEHub
    from fleet.models import Event

    h = SSEHub()
    sub_a = h.subscribe("scope-a")
    sub_b = h.subscribe("scope-b")

    ev_a = Event(
        id=1, ts="2026-01-01T00:00:00Z", scope="scope-a",
        type="t", summary="for-a", payload={},
    )
    await h.publish("scope-a", ev_a)

    # sub_a gets it
    received_a: list[Event] = []
    async for e in sub_a:
        received_a.append(e)
        break
    h.unsubscribe("scope-a", sub_a)

    # sub_b queue should be empty — timeout immediately
    received_b: list[Event] = []
    try:
        async def _drain_b() -> None:
            async for e in sub_b:
                received_b.append(e)
                break

        await asyncio.wait_for(_drain_b(), timeout=0.1)
    except TimeoutError:
        pass
    finally:
        h.unsubscribe("scope-b", sub_b)

    assert len(received_a) == 1
    assert len(received_b) == 0


# ---------------------------------------------------------------------------
# Integration tests — HTTP endpoints + SSE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_event_endpoint(app_client: AsyncClient) -> None:
    """POST /api/events creates an event and returns its id."""
    resp = await app_client.post(
        "/api/events",
        json={"scope": "integ", "type": "task.created", "summary": "hello"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "id" in body
    assert isinstance(body["id"], int)
    assert body["id"] > 0


@pytest.mark.asyncio
async def test_get_events_endpoint(app_client: AsyncClient) -> None:
    """GET /api/events returns posted events filtered by scope."""
    await app_client.post(
        "/api/events",
        json={"scope": "integ-get", "type": "t", "summary": "e1"},
    )
    await app_client.post(
        "/api/events",
        json={"scope": "other", "type": "t", "summary": "e2"},
    )
    resp = await app_client.get("/api/events", params={"scope": "integ-get"})
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 1
    assert events[0]["scope"] == "integ-get"


@pytest.mark.asyncio
async def test_auth_rejects_missing_token(tmp_path: pytest.TempPathFactory) -> None:
    """Without auth override, missing Bearer token → 401."""
    from fleet.api.events import router
    from fleet.events.service import create_event_service
    from fleet.events.sse import SSEHub

    db_path = str(tmp_path / "auth_test.db")
    manager = await init_db(db_path)
    svc = create_event_service(manager, SSEHub())

    app = FastAPI()
    app.include_router(router)
    app.state.event_service = svc
    app.state.sse_hub = SSEHub()

    # Set a real token in the environment so require_token validates against it
    os.environ["FLEET_API_TOKEN"] = "secret-token"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/events",
                json={"scope": "s", "type": "t", "summary": "x"},
            )
            assert resp.status_code == 401
    finally:
        del os.environ["FLEET_API_TOKEN"]
        await manager.close()


@pytest.mark.asyncio
async def test_auth_accepts_correct_token(tmp_path: pytest.TempPathFactory) -> None:
    """Correct Bearer token → 200."""
    from fleet.api.events import router
    from fleet.events.service import create_event_service
    from fleet.events.sse import SSEHub

    db_path = str(tmp_path / "auth_ok_test.db")
    manager = await init_db(db_path)
    svc = create_event_service(manager, SSEHub())

    app = FastAPI()
    app.include_router(router)
    app.state.event_service = svc
    app.state.sse_hub = SSEHub()

    os.environ["FLEET_API_TOKEN"] = "my-token"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/events",
                headers={"Authorization": "Bearer my-token"},
                json={"scope": "s", "type": "t", "summary": "x"},
            )
            assert resp.status_code == 200
    finally:
        del os.environ["FLEET_API_TOKEN"]
        await manager.close()


async def _run_server(
    app: FastAPI, host: str, port: int, started: asyncio.Event
) -> None:
    """Run uvicorn in-process; set *started* once ready to accept connections."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="error")
    server = uvicorn.Server(config)

    # Monkey-patch startup to set our event
    original_startup = server.startup

    async def _startup(sockets: object = None) -> None:
        await original_startup(sockets)
        started.set()

    server.startup = _startup  # type: ignore[method-assign]
    await server.serve()


@asynccontextmanager
async def _live_server(app: FastAPI, port: int) -> AsyncIterator[str]:  # noqa: F821
    """Async context manager: start uvicorn, yield base_url, then shut it down."""
    started = asyncio.Event()
    server_task = asyncio.create_task(
        _run_server(app, "127.0.0.1", port, started)
    )
    await asyncio.wait_for(started.wait(), timeout=10.0)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


def _build_sse_app(db_path: str) -> FastAPI:  # noqa: F821
    """Build a self-contained FastAPI app for SSE integration tests.

    State is initialised in the app's lifespan so it shares the same
    event loop as the uvicorn server.
    """
    from collections.abc import AsyncIterator as _AI
    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    from fleet.api.auth import require_token
    from fleet.api.events import router
    from fleet.events.service import create_event_service
    from fleet.events.sse import SSEHub

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> _AI[None]:
        sse_hub = SSEHub()
        manager = await init_db(db_path)
        svc = create_event_service(manager, sse_hub)
        application.state.event_service = svc
        application.state.sse_hub = sse_hub
        yield
        await manager.close()

    async def _no_auth() -> None:
        return None

    application = FastAPI(lifespan=lifespan)
    application.include_router(router)
    application.dependency_overrides[require_token] = _no_auth
    return application


@pytest.mark.asyncio
async def test_sse_catchup_on_reconnect(tmp_path: pytest.TempPathFactory) -> None:
    """SSE stream with Last-Event-ID=0 replays all 3 events, then streams 1 live.

    Uses a real uvicorn server so the SSE stream, the append, and the
    HTTP client all run without blocking each other.  A live server avoids
    the httpx ASGITransport deadlock (that transport buffers the whole
    response before returning, making infinite SSE streams hang).
    """
    import socket

    db_path = str(tmp_path / "sse_catchup.db")

    # Pick a free port
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    app = _build_sse_app(db_path)
    collected: list[dict] = []

    async with _live_server(app, port) as base_url:
        svc: EventService = app.state.event_service  # type: ignore[assignment]

        # Append 3 events before connecting (these become catch-up events)
        await svc.append("catchup", "t", "e1")
        await svc.append("catchup", "t", "e2")
        await svc.append("catchup", "t", "e3")

        async with AsyncClient(base_url=base_url, timeout=10.0) as client:

            async def read_stream() -> None:
                async with client.stream(
                    "GET",
                    "/api/events/stream",
                    params={"scope": "catchup"},
                    headers={"Last-Event-ID": "0"},
                ) as resp:
                    assert resp.status_code == 200
                    # Append live event once connected
                    await svc.append("catchup", "t", "e4-live")
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data = json.loads(line[len("data:"):].strip())
                            collected.append(data)
                            if len(collected) >= 4:
                                break

            await asyncio.wait_for(read_stream(), timeout=8.0)

    assert len(collected) == 4, f"Expected 4 events, got {collected}"
    assert collected[0]["summary"] == "e1"
    assert collected[1]["summary"] == "e2"
    assert collected[2]["summary"] == "e3"
    assert collected[3]["summary"] == "e4-live"


@pytest.mark.asyncio
async def test_sse_live_stream(tmp_path: pytest.TempPathFactory) -> None:
    """After subscribing to SSE stream, appending an event delivers it within 2 s.

    Uses a real uvicorn server for the same reason as test_sse_catchup_on_reconnect.
    """
    import socket

    db_path = str(tmp_path / "sse_live.db")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    app = _build_sse_app(db_path)
    collected: list[dict] = []
    connected = asyncio.Event()

    async with _live_server(app, port) as base_url:
        svc: EventService = app.state.event_service  # type: ignore[assignment]

        async def consume() -> None:
            async with AsyncClient(base_url=base_url, timeout=10.0) as client:
                async with client.stream(
                    "GET",
                    "/api/events/stream",
                    params={"scope": "live-test"},
                ) as resp:
                    assert resp.status_code == 200
                    connected.set()
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            data = json.loads(line[len("data:"):].strip())
                            collected.append(data)
                            break

        consume_task = asyncio.create_task(consume())
        await asyncio.wait_for(connected.wait(), timeout=5.0)
        # Brief pause so the SSE generator registers the subscription
        await asyncio.sleep(0.1)
        await svc.append("live-test", "task.done", "live-event")

        await asyncio.wait_for(consume_task, timeout=5.0)

    assert len(collected) == 1
    assert collected[0]["summary"] == "live-event"
