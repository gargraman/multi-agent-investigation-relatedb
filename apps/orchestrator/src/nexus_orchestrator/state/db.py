"""SQLite access (spec §8.7, ADR-002 A3, ADR-005 D2).

- ``aiosqlite`` only: no blocking SQLite call ever runs on the event loop.
- WAL, ``busy_timeout=5000``, ``synchronous=NORMAL``, ``foreign_keys=ON`` on every connection.
- Exactly **one write connection**, serialized behind an ``asyncio.Lock``; reads use a separate
  connection. Every write goes through :meth:`Database.transaction` (``BEGIN IMMEDIATE``).
- Callers must not make network calls inside a transaction. Post-commit side effects (SSE
  publish) are registered with :meth:`Transaction.after_commit`; they run only after ``COMMIT``
  succeeds and still under the write lock, so subscribers see events in ``seq`` order.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)

_INV_STATUSES = "'CREATED','RUNNING','ANALYZING','COMPLETED','PARTIAL','FAILED','CANCELLED'"
_TASK_STATUSES = "'PENDING','DISPATCHED','RUNNING','COMPLETED','FAILED','TIMED_OUT','CANCELLED'"

# Exactly the §8.7 columns. Idempotent: safe to run on every start.
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS investigations (
    id                 TEXT PRIMARY KEY,
    scenario_id        TEXT NOT NULL,
    objective          TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ({_INV_STATUSES})),
    status_reason      TEXT,
    tokens_used        INTEGER NOT NULL DEFAULT 0,
    token_budget       INTEGER NOT NULL,
    deadline           TEXT NOT NULL,
    assessment_id      TEXT,
    context_closed     INTEGER NOT NULL DEFAULT 0 CHECK (context_closed IN (0, 1)),
    trace_context_json TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_investigations_created ON investigations (created_at, id);
CREATE INDEX IF NOT EXISTS ix_investigations_status ON investigations (status);

CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    investigation_id  TEXT NOT NULL REFERENCES investigations (id),
    agent_id          TEXT NOT NULL,
    task_type         TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ({_TASK_STATUSES})),
    status_reason     TEXT,
    attempt           INTEGER NOT NULL DEFAULT 1,
    deadline          TEXT NOT NULL,
    token_reservation INTEGER NOT NULL,
    acked_at          TEXT,
    finding_ids_json  TEXT NOT NULL DEFAULT '[]',
    usage_json        TEXT,
    error_category    TEXT,
    error_code        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_tasks_investigation ON tasks (investigation_id);

CREATE TABLE IF NOT EXISTS leader (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    holder       TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_events (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    investigation_id TEXT NOT NULL REFERENCES investigations (id),
    type             TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_timeline_investigation_seq ON timeline_events (investigation_id, seq);
"""


class Transaction:
    """Handle for one open write transaction."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self._after_commit: list[Callable[[], None]] = []

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, tuple(params))

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()

    def after_commit(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` (sync, cheap, no I/O) only if this transaction commits."""
        self._after_commit.append(fn)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._writer: aiosqlite.Connection | None = None
        self._reader: aiosqlite.Connection | None = None
        self._write_lock: asyncio.Lock | None = None  # created in open(), on the running loop

    @property
    def is_open(self) -> bool:
        return self._writer is not None and self._reader is not None

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        for pragma in PRAGMAS:
            await conn.execute(pragma)
        return conn

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()
        self._writer = await self._connect()
        await self._writer.executescript(SCHEMA)
        self._reader = await self._connect()

    async def close(self) -> None:
        for conn in (self._reader, self._writer):
            if conn is not None:
                try:
                    if conn.in_transaction:
                        await conn.execute("ROLLBACK")
                finally:
                    await conn.close()
        self._reader = self._writer = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """``BEGIN IMMEDIATE`` … ``COMMIT`` on the single writer. Rolls back on any error."""
        assert self._writer is not None and self._write_lock is not None, "database not open"
        async with self._write_lock:
            conn = self._writer
            await conn.execute("BEGIN IMMEDIATE")
            tx = Transaction(conn)
            try:
                yield tx
                await self._commit(conn)
            except BaseException:
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
                raise
            for fn in tx._after_commit:
                try:
                    fn()
                except Exception:  # a broken subscriber must not undo a committed write
                    log.exception("post-commit hook failed")

    async def _commit(self, conn: aiosqlite.Connection) -> None:
        """Separate method so tests can inject a commit failure."""
        await conn.execute("COMMIT")

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        assert self._reader is not None, "database not open"
        async with self._reader.execute(sql, tuple(params)) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        assert self._reader is not None, "database not open"
        async with self._reader.execute(sql, tuple(params)) as cur:
            return await cur.fetchone()
