"""PENDING tasks are re-POSTed every tick until their deadline (S3 spec §12 row 1, FR-003,
FR-120, NFR-101).

- The first dispatch of an attempt runs the full FR-003 retry loop; every later tick sends one
  try per PENDING task (any attempt), never the loop again.
- A PENDING task at or past its deadline is not re-POSTed: the deadline watcher owns it.
- Before each send the row is re-read: once it is no longer PENDING (fence, cancel, timeout) the
  dispatch stops.
- Dispatch runs off the tick, so an agent whose submit hangs cannot hold back the deadline watcher
  for the other tasks.

Same harness as ``test_failure_matrix``: two ``FakeAgent`` doubles, one ``FakeClock``, one tick per
5 virtual seconds.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from orch_testkit import runtime, wait_until

from nexus_contracts.api.states import InvestigationStatus as I
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_contracts.common import AgentId
from nexus_orchestrator.workflow import deadlines

SIG, GEO = AgentId.SIGINT, AgentId.GEOINT
TASK_DEADLINE = 180.0
FIRST_LOOP_TRIES = 4  # 1 + dispatch_max_retries (3)


async def _unreachable_noted(s, inv_id: str) -> None:
    """Drive until the first dispatch's retry loop has given up (its timeline note)."""
    for _ in range(20):
        resp = await s.client.get(
            f"/api/v1/investigations/{inv_id}/timeline", params={"limit": 100}
        )
        if any(
            e["type"] == "transition.dropped" and e["payload"].get("reason") == "agent_unreachable"
            for e in resp.json()["items"]
        ):
            return
        await s.drive(5)
    raise AssertionError("first dispatch never gave up")


async def test_attempt1_pending_dispatched_when_agent_returns(chaos_app) -> None:
    """Agent down on the first dispatch, up two ticks later: attempt 1 is DISPATCHED on the next
    tick, long before its deadline, with no re-dispatch."""
    chaos_app.geo.down()
    async with chaos_app.session() as s:
        inv_id = await s.create()
        created = chaos_app.clock.now()
        await _unreachable_noted(s, inv_id)
        await s.drive(10)
        assert (await s.tasks(inv_id))[GEO].status is T.PENDING
        chaos_app.geo.bring_up()
        await s.drive(5)
        geo = (await s.tasks(inv_id))[GEO]
        elapsed = chaos_app.clock.now() - created

    assert geo.attempt == 1
    assert geo.status in {T.DISPATCHED, T.RUNNING, T.COMPLETED}
    assert elapsed < timedelta(seconds=TASK_DEADLINE / 2)
    assert chaos_app.geo.statuses() == [202]


async def test_later_tick_repost_is_one_send_per_tick(chaos_app) -> None:
    """After the first dispatch's retry loop, each tick makes exactly one send for the task."""
    chaos_app.geo.lose_response = True  # the agent sees every POST, the replies are lost
    async with chaos_app.session() as s:
        inv_id = await s.create()
        await _unreachable_noted(s, inv_id)
        first = len(chaos_app.geo.submits)
        counts = []
        for _ in range(3):
            await s.drive(5)
            counts.append(len(chaos_app.geo.submits))
        assert (await s.tasks(inv_id))[GEO].status is T.PENDING

    assert first == FIRST_LOOP_TRIES
    assert counts == [first + 1, first + 2, first + 3]


async def test_expired_pending_task_is_not_reposted(chaos_app, monkeypatch) -> None:
    """With the watcher switched off, a PENDING task past its deadline is no longer re-POSTed."""
    monkeypatch.setattr(deadlines, "expired", lambda tasks, now: [])
    chaos_app.geo.lose_response = True
    async with chaos_app.session() as s:
        inv_id = await s.create()
        deadline = (await s.tasks(inv_id))[GEO].deadline
        await _unreachable_noted(s, inv_id)
        while chaos_app.clock.now() < deadline:
            await s.drive(5)
        at_deadline = len(chaos_app.geo.submits)
        await s.drive(30)
        after = len(chaos_app.geo.submits)
        assert (await s.tasks(inv_id))[GEO].status is T.PENDING

    assert at_deadline > FIRST_LOOP_TRIES  # it was re-POSTed while it had time left
    assert after == at_deadline


async def test_cancel_between_retry_tries_stops_the_loop(chaos_app) -> None:
    """A cancel (fence) that lands while the first dispatch waits between tries: no further
    send for that task."""
    chaos_app.geo.lose_response = True
    async with chaos_app.session() as s:
        inv_id = await s.create()
        await s.settle()  # first try sent, the retry loop is in its backoff sleep
        assert len(chaos_app.geo.submits) == 1
        resp = await s.client.post(f"/api/v1/investigations/{inv_id}/cancel")
        assert resp.status_code == 200, resp.text
        await s.drive(30)
        inv = await s.detail(inv_id)

    assert inv.status is I.CANCELLED
    assert len(chaos_app.geo.submits) == 1


async def test_timed_out_between_retry_tries_stops_the_loop(chaos_factory) -> None:
    """Same with the task deadline: the row is TIMED_OUT while the loop sleeps, so it stops."""
    backoff = 30.0
    chaos = chaos_factory(task_deadline_seconds=3, dispatch_backoff_seconds=backoff)
    chaos.geo.lose_response = True  # /readyz answers are lost too: no attempt 2
    async with chaos.session() as s:
        inv_id = await s.create()
        created = chaos.clock.now()
        await s.settle()
        assert len(chaos.geo.submits) == 1
        await s.drive(15)  # the next ticks: the task is past its 3 s deadline -> TIMED_OUT
        changes = await s.changes(inv_id, GEO)
        assert (1, "PENDING", "TIMED_OUT", "deadline_exceeded") in changes
        assert chaos.clock.now() - created < timedelta(seconds=backoff)  # loop still asleep
        await s.drive(backoff)  # the loop wakes, re-reads the row and stops
        geo = (await s.tasks(inv_id))[GEO]

    attempt1 = [b for _, b, _ in chaos.geo.submits if b.attempt == 1]
    assert len(attempt1) == 1
    assert geo.attempt == 2 or geo.status is T.TIMED_OUT


async def test_hanging_submit_does_not_hold_the_other_tasks_deadline(chaos_app) -> None:
    """GEOINT's submit never answers; SIGINT acknowledges and then never finishes. SIGINT's
    timeout is still applied within one poll interval of its deadline (NFR-101)."""
    gate = asyncio.Event()
    chaos_app.geo.submit_gate = gate
    chaos_app.sig.hang()
    async with chaos_app.session() as s:
        inv_id = await s.create()
        rt = runtime(s.app)
        deadline = (await s.tasks(inv_id))[SIG].deadline
        seen_at = None
        try:
            while chaos_app.clock.now() < deadline + timedelta(seconds=3 * chaos_app.poll):
                await _parked(chaos_app.clock, rt)
                changes = await s.changes(inv_id, SIG)
                if (1, "RUNNING", "TIMED_OUT", "deadline_exceeded") in changes or (
                    1,
                    "DISPATCHED",
                    "TIMED_OUT",
                    "deadline_exceeded",
                ) in changes:
                    seen_at = chaos_app.clock.now()
                    break
                chaos_app.clock.advance(1.0)
            geo = (await s.tasks(inv_id))[GEO]
        finally:
            gate.set()

    # Still hanging: attempt 1 was never acknowledged, and no attempt 2 went out while its
    # submit was in flight (a re-dispatch waits for the task's job to end).
    assert chaos_app.geo.submit_entered == 1
    assert geo.status in {T.PENDING, T.TIMED_OUT}
    assert seen_at is not None, "SIGINT's deadline was never applied"
    assert seen_at - deadline <= timedelta(seconds=chaos_app.poll)


async def _parked(clock, rt) -> None:
    """Every driver (and the reconciler) sleeps on the clock. Unlike ``Session.settle`` this does
    not wait for in-flight dispatches: one of them is meant to hang."""
    end = time.monotonic() + 5.0
    while clock.pending() < 1 + len(rt.poller.active()):
        if time.monotonic() > end:
            raise AssertionError("drivers did not park")
        await asyncio.sleep(0.002)
    for _ in range(5):
        await asyncio.sleep(0.002)


async def test_ack_for_an_attempt_that_is_over_is_ignored(chaos_app) -> None:
    """Attempt 1's submit is in flight when the row moves on to attempt 2 (PENDING again): the
    late ack must not mark attempt 2 DISPATCHED, and no cancel (by task id) is sent."""
    gate = asyncio.Event()
    chaos_app.geo.submit_gate = gate
    async with chaos_app.session() as s:
        inv_id = await s.create()
        rt = runtime(s.app)

        async def entered() -> bool:
            return chaos_app.geo.submit_entered == 1

        await wait_until(entered)
        geo = (await s.tasks(inv_id))[GEO]
        async with rt.db.transaction() as tx:  # as a timeout + re-dispatch would have left it
            await tx.execute(
                "UPDATE tasks SET attempt = 2, version = version + 2 WHERE id = ?", (geo.id,)
            )
        gate.set()

        async def job_done() -> bool:
            return rt.poller.dispatches_busy() == 0 and chaos_app.geo.statuses() == [202]

        await wait_until(job_done)
        await asyncio.sleep(0.05)
        after = (await s.tasks(inv_id))[GEO]

    assert (after.attempt, after.status) == (2, T.PENDING)
    assert chaos_app.geo.cancels == []


async def test_late_rejection_for_an_attempt_that_is_over_is_ignored(chaos_app) -> None:
    """Attempt 1's submit is in flight when the row moves on to attempt 2 (PENDING again); the
    agent then answers REJECTED. That verdict is about attempt 1: attempt 2 stays PENDING and is
    not failed without ever being sent."""
    from nexus_contracts.a2a.task import AgentTaskStatus

    gate = asyncio.Event()
    chaos_app.geo.submit_gate = gate
    chaos_app.geo.submit_status = AgentTaskStatus.REJECTED
    async with chaos_app.session() as s:
        inv_id = await s.create()
        rt = runtime(s.app)

        async def entered() -> bool:
            return chaos_app.geo.submit_entered == 1

        await wait_until(entered)
        geo = (await s.tasks(inv_id))[GEO]
        async with rt.db.transaction() as tx:  # as a timeout + re-dispatch would have left it
            await tx.execute(
                "UPDATE tasks SET attempt = 2, version = version + 2 WHERE id = ?", (geo.id,)
            )
        gate.set()

        async def job_done() -> bool:
            return rt.poller.dispatches_busy() == 0 and len(chaos_app.geo.statuses()) >= 1

        await wait_until(job_done)
        await asyncio.sleep(0.05)
        after = (await s.tasks(inv_id))[GEO]

    assert (after.attempt, after.status) == (2, T.PENDING)


async def test_refusal_write_for_an_attempt_that_is_over_is_dropped(chaos_app) -> None:
    """The refusal write itself (4xx / REJECTED → FAILED) carries the attempt it is about: a
    snapshot of attempt 1 cannot fail the row once attempt 2 owns it."""
    from nexus_contracts.api.errors import ErrorCode

    chaos_app.geo.submit_gate = asyncio.Event()  # keep the real dispatch out of the way
    async with chaos_app.session() as s:
        inv_id = await s.create()
        rt = runtime(s.app)
        stale = (await s.tasks(inv_id))[GEO]
        async with rt.db.transaction() as tx:
            await tx.execute(
                "UPDATE tasks SET attempt = 2, version = version + 2 WHERE id = ?", (stale.id,)
            )
        await rt.poller.dispatcher._fail(stale, ErrorCode.TASK_REJECTED)
        after = (await s.tasks(inv_id))[GEO]
        chaos_app.geo.submit_gate.set()

    assert (after.attempt, after.status) == (2, T.PENDING)


async def test_poll_or_timeout_write_for_an_attempt_that_is_over_is_dropped(chaos_app) -> None:
    """Same for the driver's own writes (a poll result, a timeout): a snapshot of attempt 1 (a poll
    job that outlived its leader, say) cannot move the row once attempt 2 owns it (FR-122)."""
    from nexus_contracts.a2a.status_mapping import TaskStateUpdate
    from nexus_contracts.api.errors import ErrorCode
    from nexus_orchestrator.state.repository import Applied

    chaos_app.geo.submit_gate = asyncio.Event()  # keep the real dispatch out of the way
    async with chaos_app.session() as s:
        inv_id = await s.create()
        rt = runtime(s.app)
        stale = (await s.tasks(inv_id))[GEO]
        async with rt.db.transaction() as tx:
            await tx.execute("UPDATE tasks SET attempt = 2 WHERE id = ?", (stale.id,))
        update = TaskStateUpdate(T.TIMED_OUT, ErrorCode.DEADLINE_EXCEEDED)
        applied = await rt.poller._record(stale, update, None, None)
        after = (await s.tasks(inv_id))[GEO]
        chaos_app.geo.submit_gate.set()

    assert applied is Applied.DROPPED
    assert (after.attempt, after.status) == (2, T.PENDING)
