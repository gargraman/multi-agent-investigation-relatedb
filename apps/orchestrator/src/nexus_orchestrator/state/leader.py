"""SQLite leader lock (ADR-005 D1, ADR-008 F3, spec §6.2).

One ``leader`` row (``id = 1``). A process claims it in a ``BEGIN IMMEDIATE`` transaction when
the row is absent, already its own, or its heartbeat is stale (older than ``stale_seconds``).
Otherwise it re-checks every ``poll_seconds`` and gives up after ``wait_seconds`` with
``another orchestrator instance holds the leader lock``. While held, a heartbeat task refreshes
``heartbeat_at`` every ``heartbeat_seconds``.

Timestamps are wall-clock UTC because they are compared across processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass

from nexus_orchestrator.ids import iso, parse_iso, utcnow
from nexus_orchestrator.state.db import Database

log = logging.getLogger(__name__)

LOCK_HELD_MESSAGE = "another orchestrator instance holds the leader lock"


class LeaderLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaderTimings:
    heartbeat_seconds: float = 5.0
    stale_seconds: float = 15.0
    wait_seconds: float = 30.0
    poll_seconds: float = 1.0


def default_holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LeaderLock:
    def __init__(self, db: Database, timings: LeaderTimings, holder: str | None = None) -> None:
        self.db = db
        self.timings = timings
        self.holder = holder or default_holder_id()
        self._held = False
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def held(self) -> bool:
        return self._held

    async def try_claim(self) -> bool:
        """One claim attempt in a single short ``BEGIN IMMEDIATE`` transaction."""
        now = utcnow()
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT holder, heartbeat_at FROM leader WHERE id = 1")
            if row is None:
                await tx.execute(
                    "INSERT INTO leader (id, holder, heartbeat_at) VALUES (1, ?, ?)",
                    (self.holder, iso(now)),
                )
                return True
            age = (now - parse_iso(row["heartbeat_at"])).total_seconds()
            if row["holder"] != self.holder and age <= self.timings.stale_seconds:
                return False
            if row["holder"] != self.holder:
                log.warning(
                    "taking over stale leader lock",
                    extra={"previous_holder": row["holder"], "heartbeat_age_s": round(age, 1)},
                )
            await tx.execute(
                "UPDATE leader SET holder = ?, heartbeat_at = ? WHERE id = 1",
                (self.holder, iso(now)),
            )
            return True

    async def acquire(self) -> None:
        """Claim the lock, waiting up to ``wait_seconds``; then start the heartbeat."""
        deadline = time.monotonic() + self.timings.wait_seconds
        while not await self.try_claim():
            if time.monotonic() >= deadline:
                raise LeaderLockError(LOCK_HELD_MESSAGE)
            log.info("leader lock held by another instance; waiting")
            await asyncio.sleep(self.timings.poll_seconds)
        self._held = True
        log.info("leader lock acquired", extra={"holder": self.holder})
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="leader-heartbeat")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.timings.heartbeat_seconds)
            try:
                async with self.db.transaction() as tx:
                    cur = await tx.execute(
                        "UPDATE leader SET heartbeat_at = ? WHERE id = 1 AND holder = ?",
                        (iso(utcnow()), self.holder),
                    )
                    lost = cur.rowcount == 0
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("leader heartbeat failed; will retry")
                continue
            if lost:
                # Someone took over (we were stale). Stop claiming to lead; /readyz goes red.
                self._held = False
                log.critical("leader lock lost to another instance")
                return

    async def release(self) -> None:
        """Stop the heartbeat and free the row so a restart need not wait for staleness."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._held and self.db.is_open:
            async with self.db.transaction() as tx:
                await tx.execute("DELETE FROM leader WHERE id = 1 AND holder = ?", (self.holder,))
        self._held = False
