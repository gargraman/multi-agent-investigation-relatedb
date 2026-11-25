"""SQLite schema (spec §8.7), pragmas (ADR-002 A3) and leader lock (ADR-005 D1, ADR-008 F3)."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

import pytest
from orch_testkit import FakeContextService, running

from nexus_orchestrator.ids import iso, utcnow
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.leader import (
    LOCK_HELD_MESSAGE,
    LeaderLock,
    LeaderLockError,
    LeaderTimings,
)

SPEC_COLUMNS = {
    "investigations": [
        "id",
        "scenario_id",
        "objective",
        "status",
        "status_reason",
        "tokens_used",
        "token_budget",
        "deadline",
        "assessment_id",
        "context_closed",
        "trace_context_json",
        "created_at",
        "updated_at",
        "version",
    ],
    "tasks": [
        "id",
        "investigation_id",
        "agent_id",
        "task_type",
        "status",
        "status_reason",
        "attempt",
        "deadline",
        "token_reservation",
        "acked_at",
        "finding_ids_json",
        "usage_json",
        "error_category",
        "error_code",
        "created_at",
        "updated_at",
        "version",
        "llm_mode",  # S2 addition (TaskView.llm_mode); not in §8.7, appended last
    ],
    "leader": ["id", "holder", "heartbeat_at"],
    "timeline_events": ["seq", "investigation_id", "type", "payload_json", "created_at"],
}

FAST = LeaderTimings(heartbeat_seconds=0.05, stale_seconds=0.3, wait_seconds=0.3, poll_seconds=0.02)
# Fresh-lock tests must never see the holder as stale. Both "processes" share one event loop
# here, so under CPU load a heartbeat gap > 0.3 s made a live holder look dead and the second
# instance took over (a real flake). Staleness itself is tested separately below.
FRESH = LeaderTimings(heartbeat_seconds=0.05, stale_seconds=30, wait_seconds=0.3, poll_seconds=0.02)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "s.sqlite")
    await d.open()
    yield d
    await d.close()


async def test_schema_has_exactly_the_spec_columns_and_is_idempotent(db, tmp_path) -> None:
    for table, columns in SPEC_COLUMNS.items():
        rows = await db.fetchall(f"PRAGMA table_info({table})")
        assert [r["name"] for r in rows] == columns, table
    pk = await db.fetchall("PRAGMA table_info(timeline_events)")
    assert pk[0]["pk"] == 1
    fks = await db.fetchall("PRAGMA foreign_key_list(tasks)")
    assert fks[0]["table"] == "investigations"
    # Re-opening re-runs the DDL without error.
    again = Database(tmp_path / "s.sqlite")
    await again.open()
    await again.close()


async def test_pragmas(db) -> None:
    assert (await db.fetchone("PRAGMA journal_mode"))[0] == "wal"
    assert (await db.fetchone("PRAGMA busy_timeout"))[0] == 5000
    assert (await db.fetchone("PRAGMA foreign_keys"))[0] == 1


async def test_leader_row_is_singleton(db) -> None:
    async with db.transaction() as tx:
        await tx.execute("INSERT INTO leader VALUES (1, 'a', ?)", (iso(utcnow()),))
    with pytest.raises(Exception):  # noqa: B017 - sqlite3.IntegrityError via aiosqlite
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO leader VALUES (2, 'b', ?)", (iso(utcnow()),))


async def test_second_instance_blocked_by_fresh_lock(tmp_path) -> None:
    a, b = Database(tmp_path / "l.sqlite"), Database(tmp_path / "l.sqlite")
    await a.open()
    await b.open()
    first = LeaderLock(a, FRESH, holder="first")
    second = LeaderLock(b, FRESH, holder="second")
    try:
        await first.acquire()
        with pytest.raises(LeaderLockError, match=LOCK_HELD_MESSAGE):
            await second.acquire()  # first keeps heartbeating, so it never goes stale
        assert first.held and not second.held
    finally:
        await first.release()
        await a.close()
        await b.close()


async def test_stale_lock_is_taken_over(db) -> None:
    async with db.transaction() as tx:
        await tx.execute(
            "INSERT INTO leader VALUES (1, 'dead-process', ?)",
            (iso(utcnow() - timedelta(seconds=60)),),
        )
    lock = LeaderLock(db, FAST, holder="new")
    await lock.acquire()
    try:
        row = await db.fetchone("SELECT holder FROM leader WHERE id = 1")
        assert row["holder"] == "new"
    finally:
        await lock.release()


async def test_waiting_instance_takes_over_after_kill9(tmp_path) -> None:
    """kill -9 (no release, heartbeat stops) → a waiting process takes over once stale."""
    a, b = Database(tmp_path / "k.sqlite"), Database(tmp_path / "k.sqlite")
    await a.open()
    await b.open()
    timings = LeaderTimings(
        heartbeat_seconds=0.05, stale_seconds=0.2, wait_seconds=3, poll_seconds=0.02
    )
    first = LeaderLock(a, timings, holder="first")
    second = LeaderLock(b, timings, holder="second")
    try:
        await first.acquire()
        waiter = asyncio.create_task(second.acquire())
        await asyncio.sleep(0.15)
        assert not waiter.done()
        first._heartbeat_task.cancel()  # simulate kill -9: heartbeat stops, row stays
        await asyncio.wait_for(waiter, timeout=3)
        assert second.held
    finally:
        await second.release()
        await a.close()
        await b.close()


async def test_second_app_fails_startup_while_first_holds_lock(make_app) -> None:
    fake = FakeContextService()
    first = make_app(fake)
    async with running(first) as client:
        assert (await client.get("/readyz")).status_code == 200
        second = make_app(fake, leader_stale_seconds=30)  # see FRESH
        with pytest.raises(LeaderLockError):
            async with running(second):
                pass
    # Graceful shutdown released the row: a new instance starts immediately.
    third = make_app(fake, leader_wait_seconds=0)
    async with running(third) as client:
        assert (await client.get("/readyz")).status_code == 200


@pytest.mark.parametrize("yields", [0, 1, 2, 3, 5, 8])
async def test_cancelled_transaction_never_leaves_the_writer_inside_a_transaction(
    db, yields: int
) -> None:
    """Root cause of the leader-lock flake: a cancel while ``BEGIN IMMEDIATE`` (or COMMIT) is
    still queued on aiosqlite's worker thread left the writer mid-transaction, so the next
    ``BEGIN`` (the leader release) failed with "cannot start a transaction within a
    transaction" and the leader row was never deleted."""

    async def heartbeat_like() -> None:
        async with db.transaction() as tx:
            await tx.execute("INSERT OR REPLACE INTO leader VALUES (1, 'x', ?)", (iso(utcnow()),))

    task = asyncio.create_task(heartbeat_like())
    for _ in range(yields):
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert not db._writer.in_transaction
    async with db.transaction() as tx:  # the writer is usable again
        await tx.execute("DELETE FROM leader")
