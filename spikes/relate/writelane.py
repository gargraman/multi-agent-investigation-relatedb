"""Prototype of the spec §4.3 write lane: one dedicated thread owns the Relate
connection; async FastAPI handlers submit domain operations to it.

Each domain operation is a callable `fn(conn) -> result` run as ONE transaction.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Callable

import relate


class WriteLane:
    def __init__(self, path: str, bootstrap: Callable[[relate.Connection], None] | None = None):
        self._ex = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="relate-lane")
        self._path = path
        self._conn: relate.Connection | None = None
        self.thread_ids: set[int] = set()

        def _open():
            self._conn = relate.connect(path)
            if bootstrap:
                bootstrap(self._conn)

        self._ex.submit(_open).result()

    def _run_tx(self, fn: Callable[[relate.Connection], Any]) -> Any:
        conn = self._conn
        assert conn is not None
        self.thread_ids.add(threading.get_ident())
        conn.autocommit = False
        try:
            out = fn(conn)
            conn.commit()
            return out
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    def _run_read(self, fn: Callable[[relate.Connection], Any]) -> Any:
        self.thread_ids.add(threading.get_ident())
        return fn(self._conn)

    def submit_sync(self, fn, read: bool = False):
        return self._ex.submit(self._run_read if read else self._run_tx, fn).result()

    async def write(self, fn: Callable[[relate.Connection], Any]) -> Any:
        return await asyncio.wrap_future(self._ex.submit(self._run_tx, fn))

    async def read(self, fn: Callable[[relate.Connection], Any]) -> Any:
        return await asyncio.wrap_future(self._ex.submit(self._run_read, fn))

    def close(self) -> None:
        def _close():
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        self._ex.submit(_close).result()
        self._ex.shutdown(wait=True)
