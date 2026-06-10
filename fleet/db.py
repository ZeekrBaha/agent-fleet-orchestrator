"""Fleet database layer — ADR-001: single writer task, reads anywhere.

All writes are serialised through one asyncio.Queue consumed by a dedicated
background task.  Read connections are handed out freely.

Public API:
    init_db(db_path)          -> DatabaseManager
    DatabaseManager.write(op) -> awaitable, returns op's return value
    DatabaseManager.read_connection() -> context manager yielding Connection
    DatabaseManager.close()   -> drains queue, stops writer, closes engine
    run_migrations(conn)      -> idempotent DDL execution (also called internally)
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any, TypeVar

from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.engine import Engine

T = TypeVar("T")

# Path to the bundled migration file.
_MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"
_INIT_SQL = _MIGRATIONS_DIR / "0001_init.sql"


# ---------------------------------------------------------------------------
# Write-queue item types
# ---------------------------------------------------------------------------


@dataclass
class _WriteItem:
    """Carries one write operation and the future that will receive its result."""

    operation: Callable[[Connection], Any]
    future: asyncio.Future[Any]


@dataclass
class _StopItem:
    """Sentinel that tells the writer task to exit."""

    _marker: int = field(default=0, init=False)


_QueueItem = _WriteItem | _StopItem


# ---------------------------------------------------------------------------
# Pragma hook — applied to every new DBAPI connection
# ---------------------------------------------------------------------------


def _apply_pragmas(dbapi_conn: Any, _connection_record: Any) -> None:  # noqa: ANN401
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


def run_migrations(conn: Connection) -> None:
    """Execute the DDL migration file against *conn*.

    Idempotent: all statements use ``CREATE TABLE IF NOT EXISTS``.
    """
    sql = _INIT_SQL.read_text(encoding="utf-8")
    # Split on semicolons so each statement is executed individually;
    # this avoids driver-level "multiple statements" restrictions.
    for statement in sql.split(";"):
        stripped = statement.strip()
        if stripped:
            conn.execute(text(stripped))
    conn.commit()


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


def _make_engine(db_path: str) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _apply_pragmas)
    return engine


# ---------------------------------------------------------------------------
# DatabaseManager
# ---------------------------------------------------------------------------


class DatabaseManager:
    """Owns the SQLAlchemy engine, the write queue, and the writer task."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        self._writer_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Internal: writer coroutine
    # ------------------------------------------------------------------

    async def _writer(self) -> None:
        """Background task: serialise all DB writes."""
        # Defined once here to avoid re-allocating the function on every loop
        # iteration.
        def _run(eng: Engine, op: Callable[[Connection], Any]) -> Any:
            with eng.connect() as conn:
                return op(conn)

        while True:
            item = await self._queue.get()

            if isinstance(item, _StopItem):
                self._queue.task_done()
                break

            try:
                result = await asyncio.to_thread(_run, self._engine, item.operation)
                item.future.set_result(result)
            except Exception as exc:  # noqa: BLE001 — bubble to caller via future
                item.future.set_exception(exc)
            finally:
                self._queue.task_done()

    def _start_writer(self) -> None:
        self._writer_task = asyncio.create_task(self._writer())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def write(self, operation: Callable[[Connection], T]) -> T:
        """Queue a write operation and await its durable commit.

        *operation* receives a SQLAlchemy ``Connection``; it is responsible
        for calling ``conn.commit()`` before returning.  The return value is
        forwarded to the caller.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        await self._queue.put(_WriteItem(operation=operation, future=future))
        return await future

    @contextlib.contextmanager
    def read_connection(self) -> Generator[Connection, None, None]:
        """Yield a synchronous read-only ``Connection``.

        Multiple read connections may be open simultaneously; SQLite WAL mode
        allows concurrent readers alongside a single writer.
        """
        with self._engine.connect() as conn:
            yield conn

    async def close(self) -> None:
        """Drain the write queue, stop the writer task, and close the engine.

        Callers must ``await`` any outstanding write coroutines before calling
        ``close()``.  This method does not attempt to yield to let un-awaited
        writes enqueue themselves first — that heuristic is unreliable for
        tasks that do more than one ``await`` before reaching ``write()``.
        """
        # Guard: if _start_writer() was never called there is no task to drain.
        # Attempting queue.join() without a consumer would deadlock.
        if self._writer_task is None:
            self._engine.dispose()
            return
        # Enqueue the stop sentinel — the writer will exit after processing all
        # items ahead of it.
        await self._queue.put(_StopItem())
        # Block until every queued item (including the stop sentinel) is done.
        await self._queue.join()
        await self._writer_task
        # One final yield so that tasks whose futures were just resolved by the
        # writer can proceed past their ``await future`` and mark themselves done.
        await asyncio.sleep(0)
        self._engine.dispose()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def init_db(db_path: str) -> DatabaseManager:
    """Create the DB, run migrations, start the writer task.

    Returns a ready-to-use ``DatabaseManager``.
    """
    engine = _make_engine(db_path)

    # Run migrations synchronously (called once at startup — not a hot path).
    with engine.connect() as conn:
        run_migrations(conn)

    manager = DatabaseManager(engine)
    manager._start_writer()
    return manager
