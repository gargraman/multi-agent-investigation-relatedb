"""Task deadline watcher (S3b FR-120, FR-121b, NFR-101) on a virtual clock.

The driver sleeps on ``FakeClock``, so it only ticks when a test advances time. Effects are
DB-backed (thread hops), so tests wait on real conditions (``wait_until`` / ``pump``) instead of
counting event-loop yields. ``FakeAgent`` is a double at the HTTP boundary.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from orch_testkit import (
    SCENARIO,
    FakeAgent,
    FakeClock,
    FakeContextService,
    S,
    running,
    runtime,
    wait_until,
)

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_contracts.events.timeline import TimelineEvent
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.state.repository import TaskRecord
from nexus_orchestrator.workflow.deadlines import expired

TASK_DEADLINE_SECONDS = 180.0


async def create(client) -> str:
    resp = await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def the_task(app, inv_id: str) -> TaskRecord:
    [task] = await runtime(app).repo.task_records(inv_id)
    return task


async def events(client, inv_id: str, type_: str) -> list[TimelineEvent]:
    resp = await client.get(f"/api/v1/investigations/{inv_id}/timeline", params={"limit": 100})
    return [TimelineEvent.model_validate(e) for e in resp.json()["items"] if e["type"] == type_]


async def parked(clock: FakeClock) -> None:
    """Wait until the driver has finished its tick and is sleeping on the clock again."""

    async def sleeping() -> bool:
        return clock.pending() >= 1

    await wait_until(sleeping)


def task_in(app, inv_id: str, *states: T) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await the_task(app, inv_id)).status in states

    return check


async def pump(
    clock: FakeClock, predicate: Callable[[], Awaitable[bool]], step: float = 0.01
) -> None:
    """Advance virtual time in small steps (real pauses between them for the DB threads) until
    ``predicate`` holds."""
    end = time.monotonic() + 5.0
    while not await predicate():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        clock.advance(step)
        await asyncio.sleep(0.005)


# -- the pure helper ---------------------------------------------------------------------------


def _record(status: T, deadline: datetime) -> TaskRecord:
    return TaskRecord(
        id=f"tsk_{status.value}",
        investigation_id="inv_x",
        agent_id=FakeAgent().agent_id,
        task_type="t",
        status=status,
        status_reason=None,
        attempt=1,
        deadline=deadline,
        token_reservation=1,
        acked_at=None,
        version=1,
        updated_at=deadline,
    )


def test_expired_selects_open_tasks_at_or_past_their_deadline() -> None:
    now = utcnow()
    past, edge = now.replace(year=now.year - 1), now
    future = now.replace(year=now.year + 1)
    tasks = [
        *(_record(s, past) for s in (T.PENDING, T.DISPATCHED, T.RUNNING)),
        _record(T.RUNNING, edge),  # now >= deadline: the boundary counts
        _record(T.RUNNING, future),
        *(_record(s, past) for s in (T.COMPLETED, T.FAILED, T.TIMED_OUT, T.CANCELLED)),
    ]
    got = expired(tasks, now)
    assert [t.status for t in got] == [T.PENDING, T.DISPATCHED, T.RUNNING, T.RUNNING]
    assert tasks[4] not in got


# -- FR-120 ------------------------------------------------------------------------------------


async def test_running_task_times_out_at_deadline(make_app, exporter) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])  # acks, then stays RUNNING forever
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.RUNNING))
        await parked(clock)
        task_id = (await the_task(app, inv_id)).id

        clock.advance(TASK_DEADLINE_SECONDS)
        await wait_until(task_in(app, inv_id, T.TIMED_OUT))
        task = await the_task(app, inv_id)
        changed = await events(client, inv_id, "task.status_changed")

    assert task.status is T.TIMED_OUT and task.status_reason == "deadline_exceeded"
    [timed_out] = [e.payload for e in changed if e.payload["to_state"] == "TIMED_OUT"]
    assert timed_out["from_state"] == "RUNNING" and timed_out["reason"] == "deadline_exceeded"
    assert agent.cancels == [task_id]
    spans = [s for s in exporter.get_finished_spans() if s.name == "workflow.timeout"]
    assert len(spans) == 1
    assert spans[0].attributes["nexus.task.id"] == task_id


async def test_pending_unreachable_task_times_out_without_cancel(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent()
    agent.up = False
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)

        async def gave_up() -> bool:  # dispatch retries exhausted (the S2 G13 note)
            return bool(await events(client, inv_id, "transition.dropped"))

        await pump(clock, gave_up)
        await parked(clock)
        assert (await the_task(app, inv_id)).status is T.PENDING
        agent.up = True  # reachable now, so a stray cancel would be recorded

        clock.advance(TASK_DEADLINE_SECONDS)
        await wait_until(task_in(app, inv_id, T.TIMED_OUT))
        task = await the_task(app, inv_id)

    assert task.status_reason == "deadline_exceeded"
    assert agent.cancels == [] and agent.submits == []


async def test_timeout_visible_within_one_tick(make_app) -> None:
    """NFR-101: the deadline passes while the driver sleeps; the event appears on the next tick,
    no later than one poll interval (+ jitter) after the deadline, and never before it."""
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])
    app = make_app(
        FakeContextService(), agent, clock=clock, poll_interval_seconds=2.0, poll_jitter_ratio=0.2
    )
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.RUNNING))
        await parked(clock)
        deadline = (await the_task(app, inv_id)).deadline

        # Tick at deadline - 1 s, then reach the deadline while the driver is mid-sleep.
        clock.advance((deadline - clock.now()).total_seconds() - 1.0)
        await parked(clock)
        clock.advance(1.0)
        await asyncio.sleep(0.05)
        assert clock.now() == deadline
        assert (await the_task(app, inv_id)).status is T.RUNNING  # the driver has not woken

        await pump(clock, task_in(app, inv_id, T.TIMED_OUT), step=0.1)
        [event] = [
            e
            for e in await events(client, inv_id, "task.status_changed")
            if e.payload["to_state"] == "TIMED_OUT"
        ]
    lag = (event.created_at - deadline).total_seconds()
    assert 0 <= lag <= 3.0, lag


async def test_timeout_loses_race_to_completion(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING, S.COMPLETED])
    agent.hold = True
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.DISPATCHED))
        await parked(clock)
        stale = await the_task(app, inv_id)  # what a watcher would have read before the poll

        agent.hold = False
        await pump(clock, task_in(app, inv_id, T.COMPLETED))
        assert (await the_task(app, inv_id)).version > stale.version

        poller = runtime(app).poller
        await poller._time_out(stale)
        task = await the_task(app, inv_id)
        notes = [e.payload for e in await events(client, inv_id, "transition.dropped")]

    assert task.status is T.COMPLETED
    [note] = notes
    assert note["entity"] == "task" and note["current_state"] == "COMPLETED"
    assert note["attempted_state"] == "TIMED_OUT" and note["reason"] == "deadline_exceeded"
    assert agent.cancels == []  # nothing was timed out, so nothing is cancelled


async def test_cancel_failure_does_not_undo_the_timeout(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.RUNNING))
        await parked(clock)

        async def boom(*_a, **_k) -> bool:
            raise RuntimeError("cancel blew up")

        runtime(app).poller.agents.cancel = boom  # type: ignore[method-assign]
        clock.advance(TASK_DEADLINE_SECONDS)
        await wait_until(task_in(app, inv_id, T.TIMED_OUT))
        await wait_until(_driver_done(app, inv_id))  # the tick went on to finish the investigation
        status = (await client.get(f"/api/v1/investigations/{inv_id}")).json()["status"]
        task = await the_task(app, inv_id)
    assert task.status is T.TIMED_OUT and status == "FAILED"


def _driver_done(app, inv_id: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return inv_id not in runtime(app).poller.active()

    return check


# -- FR-121b -----------------------------------------------------------------------------------


async def test_agent_deadline_exceeded_recorded_as_timed_out_attempt1(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING, S.FAILED], fail_code=ErrorCode.DEADLINE_EXCEEDED)
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, task_in(app, inv_id, T.TIMED_OUT, T.FAILED))
        task = await the_task(app, inv_id)
        changed = [e.payload for e in await events(client, inv_id, "task.status_changed")]
        detail = (await client.get(f"/api/v1/investigations/{inv_id}")).json()

    assert task.status is T.TIMED_OUT and task.status_reason == "deadline_exceeded"
    assert changed[-1]["to_state"] == "TIMED_OUT" and changed[-1]["reason"] == "deadline_exceeded"
    assert agent.cancels == []  # the agent already ended it; this is not an orchestrator timeout
    assert detail["tokens_used"] == 150  # the attempt's usage is still real spend


async def test_agent_deadline_exceeded_attempt2_is_failed(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING, S.FAILED], fail_code=ErrorCode.DEADLINE_EXCEEDED)
    agent.hold = True
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.DISPATCHED))
        await parked(clock)
        task = await the_task(app, inv_id)
        async with runtime(app).db.transaction() as tx:  # as if this were the re-dispatch
            await tx.execute("UPDATE tasks SET attempt = 2 WHERE id = ?", (task.id,))
        agent.tasks[task.id]["attempt"] = 2

        agent.hold = False
        await pump(clock, task_in(app, inv_id, T.TIMED_OUT, T.FAILED))
        task = await the_task(app, inv_id)
        detail = (await client.get(f"/api/v1/investigations/{inv_id}")).json()

    assert task.status is T.FAILED and task.status_reason == "deadline_exceeded"
    assert detail["tasks"][0]["error_code"] == "deadline_exceeded"
