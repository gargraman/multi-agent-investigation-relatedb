"""The Relate lane (spec §4.3, ADR-001 R1/R2).

One dedicated thread owns the **only** Relate connection and serves every read and write:
Relate connections are thread-affine, a store has one writable handle, and read-only handles are
frozen snapshots (R1). Async callers submit ``fn(conn)`` and await an asyncio future; the
executor is FIFO, so operations are serialized in arrival order.

Each submitted operation is **one transaction** (``autocommit=False`` + ``commit()``), rolled back
on any exception (Relate refuses ``commit()`` after a failed statement until ``rollback()``, R2).
A background task checkpoints with ``flush()`` periodically; ``close()`` drains queued work, then
flushes and closes the store.

Every operation emits a ``relate.tx`` span with ``db.system=relate`` and ``db.operation=<domain
op>``, and nothing else: never parameters, Cypher or content (spec §15.2, §15.6).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import relate
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, TracerProvider

from nexus_context_service.settings import assert_durability_full

log = logging.getLogger(__name__)

T = TypeVar("T")
SPAN_NAME = "relate.tx"


class LaneUnavailable(RuntimeError):
    """The lane is not open (starting up or shutting down)."""


class RelateLane:
    def __init__(
        self,
        path: Path,
        *,
        flush_interval_s: float = 30.0,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._path = path
        self._flush_interval_s = flush_interval_s
        self._tracer = trace.get_tracer(__name__, tracer_provider=tracer_provider)
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._conn: relate.Connection | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    # --- lifecycle ----------------------------------------------------------------------------
    async def start(self) -> None:
        assert_durability_full()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="relate-lane"
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.wrap_future(self._executor.submit(self._open_conn))
        self._open = True
        self._flush_task = asyncio.create_task(self._flush_loop(), name="relate-flush")

    async def close(self) -> None:
        """Refuse new work, let queued work finish (FIFO), then flush + close the store."""
        if self._executor is None:
            return
        self._open = False
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        executor, self._executor = self._executor, None
        await asyncio.wrap_future(executor.submit(self._close_conn))
        executor.shutdown(wait=True)

    # --- submission ---------------------------------------------------------------------------
    async def run(
        self, operation: str, fn: Callable[[relate.Connection], T], *, transactional: bool = True
    ) -> T:
        """Run ``fn(conn)`` on the lane thread as one transaction named ``operation``.

        ``transactional=False`` runs in autocommit mode (schema DDL only)."""
        if not self._open or self._executor is None:
            raise LaneUnavailable("relate lane is not open")
        # Copy the caller's contextvars so the span is a child of the request span.
        ctx = contextvars.copy_context()
        fut = self._executor.submit(ctx.run, self._run_tx, operation, fn, transactional)
        return await asyncio.wrap_future(fut)

    # --- lane-thread internals ----------------------------------------------------------------
    def _open_conn(self) -> None:
        self._conn = relate.connect(str(self._path))

    def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.flush()
        finally:
            conn.close()  # rolls back any open tx, then checkpoints

    def _run_tx(
        self, operation: str, fn: Callable[[relate.Connection], T], transactional: bool
    ) -> T:
        conn = self._conn
        if conn is None:
            raise LaneUnavailable("relate lane is closed")
        with self._tracer.start_as_current_span(
            SPAN_NAME,
            kind=trace.SpanKind.CLIENT,
            attributes={"db.system": "relate", "db.operation": operation},
            record_exception=False,  # exception text may echo values; keep the type only
            set_status_on_exception=False,
        ) as span:
            if not transactional:
                try:
                    return fn(conn)
                except BaseException as exc:
                    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                    raise
            conn.autocommit = False
            try:
                out = fn(conn)
                conn.commit()
                return out
            except BaseException as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                try:
                    conn.rollback()
                except Exception:  # pragma: no cover - rollback failure must not mask exc
                    log.exception("relate rollback failed", extra={"operation": operation})
                raise
            finally:
                conn.autocommit = True

    def _flush(self) -> None:
        if self._conn is not None:
            self._conn.flush()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval_s)
            executor = self._executor
            if executor is None:
                return
            try:
                await asyncio.wrap_future(executor.submit(self._flush))
            except Exception:
                log.exception("relate flush failed")
