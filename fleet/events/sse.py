"""SSEHub — fan-out broadcaster for Server-Sent Events.

One SSEHub per application instance.  Subscriptions are keyed by
(scope, subscription_id).  Publish puts the event into every subscriber
queue for the matching scope; subscribers async-iterate their queue.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fleet.models import Event

_id_counter = itertools.count(1)


class Subscription:
    """Wraps an asyncio.Queue; async-iterates events as they arrive.

    Iteration stops when a ``None`` sentinel is put into the queue
    (signalled by SSEHub.unsubscribe).
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=100)
        self.id: int = next(_id_counter)

    def _put_nowait(self, event: Event | None) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # Slow subscriber — drop event rather than blocking publish

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Event]:
        while True:
            item = await self._queue.get()
            if item is None:
                # Sentinel — hub signalled shutdown for this subscription.
                break
            yield item


class SSEHub:
    """Fan-out broadcaster for Server-Sent Events.

    Thread-safety note: all public methods are called from the same asyncio
    event loop; asyncio primitives (Queue, dict) provide sufficient safety
    without explicit locks.
    """

    def __init__(self) -> None:
        # { scope: { subscription_id: Subscription } }
        self._subs: dict[str, dict[int, Subscription]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def subscribe(self, scope: str) -> Subscription:
        """Create and register a new Subscription for *scope*."""
        sub = Subscription()
        self._subs.setdefault(scope, {})[sub.id] = sub
        return sub

    async def publish(self, scope: str, event: Event) -> None:
        """Put *event* into every subscriber queue for *scope*."""
        scope_subs = self._subs.get(scope, {})
        for sub in list(scope_subs.values()):
            sub._put_nowait(event)

    def unsubscribe(self, scope: str, sub: Subscription) -> None:
        """Remove *sub* and send the stop sentinel so the iterator finishes."""
        scope_subs = self._subs.get(scope, {})
        if sub.id in scope_subs:
            del scope_subs[sub.id]
        # Signal the iterator to stop.
        sub._put_nowait(None)
