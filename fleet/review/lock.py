"""MergeLock — in-process asyncio lock, one per merge scope.

Public API:
    MergeInProgressError    — raised when a lock is already held
    MergeLock               — asyncio.Lock per scope (in-memory dict)
    MergeLock.acquire(scope) -> AsyncContextManager
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


class MergeInProgressError(Exception):
    """Raised when a concurrent merge is already in progress for a scope."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        super().__init__(f"merge in progress for scope {scope!r}")


class MergeLock:
    """Manages one asyncio.Lock per scope.

    Locks are stored in-memory; not distributed — single-process only.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def acquire(self, scope: str) -> AsyncGenerator[None, None]:
        """Acquire the lock for *scope*.

        asyncio is single-threaded and cooperative: no other coroutine can run
        between lock.locked() and lock.acquire() unless we await.  Checking
        locked() before acquiring is therefore race-free in asyncio.

        Raises:
            MergeInProgressError: If the lock for *scope* is already held.
        """
        lock = self._locks.setdefault(scope, asyncio.Lock())
        if lock.locked():
            raise MergeInProgressError(scope)
        async with lock:
            yield
