"""A frozen agent cannot hold the driver's tick (S3 spec NFR-101: a task timeout is visible within
one poll interval).

A frozen agent accepts the connection and never answers: ``docker pause``, a blocked event loop.
The 5 s poll read timeout is spec-pinned (main spec §8.3, ADR-003 B7), so the bound cannot come
from shorter timeouts. Instead every slow agent call of a tick (poll, the cancel after a timeout,
the ``/readyz`` probe before a re-dispatch) runs as a background job and the tick waits for them
at most a fraction of the poll interval, as dispatch already does.

Same harness as ``test_dispatch_ticks``: ``FakeAgent`` gates hold one request open until the test
sets them; ``FakeClock`` moves only when advanced.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from orch_testkit import runtime

from nexus_contracts.common import AgentId

SIG, GEO = AgentId.SIGINT, AgentId.GEOINT


async def _parked(clock, rt) -> None:
    """Every driver (and the reconciler) sleeps on the clock; a gated agent call may still hang."""
    end = time.monotonic() + 5.0
    while clock.pending() < 1 + len(rt.poller.active()):
        if time.monotonic() > end:
            raise AssertionError("drivers did not park: a tick is stuck on an agent call")
        await asyncio.sleep(0.002)
    for _ in range(5):
        await asyncio.sleep(0.002)


async def _sig_timed_out_at(chaos_app, s, inv_id: str):
    """Step the clock 1 s at a time until SIGINT's attempt-1 TIMED_OUT is in the timeline."""
    rt = runtime(s.app)
    deadline = (await s.tasks(inv_id))[SIG].deadline
    while chaos_app.clock.now() < deadline + timedelta(seconds=3 * chaos_app.poll):
        await _parked(chaos_app.clock, rt)
        if any(
            a == 1 and to == "TIMED_OUT" and r == "deadline_exceeded"
            for a, _, to, r in await s.changes(inv_id, SIG)
        ):
            return deadline, chaos_app.clock.now()
        chaos_app.clock.advance(1.0)
    return deadline, None


async def test_frozen_poll_does_not_hold_the_other_tasks_deadline(chaos_app) -> None:
    """GEOINT acknowledges, then freezes: its poll never answers. SIGINT's timeout still lands
    within one poll interval of its deadline."""
    chaos_app.sig.hang()
    chaos_app.geo.hang()
    gate = asyncio.Event()
    async with chaos_app.session() as s:
        inv_id = await s.create()
        await s.settle()
        chaos_app.geo.poll_gate = gate
        try:
            deadline, seen_at = await _sig_timed_out_at(chaos_app, s, inv_id)
        finally:
            gate.set()

    assert chaos_app.geo.poll_entered >= 1
    assert seen_at is not None, "SIGINT's deadline was never applied"
    assert seen_at - deadline <= timedelta(seconds=chaos_app.poll)


async def test_frozen_readyz_does_not_hold_the_other_tasks_deadline(chaos_app) -> None:
    """GEOINT fails retryably (agent restarted), and its ``/readyz`` probe before the
    re-dispatch never answers. SIGINT's timeout still lands within one poll interval."""
    chaos_app.sig.hang()
    gate = asyncio.Event()
    async with chaos_app.session() as s:
        inv_id = await s.create()
        await s.settle()
        chaos_app.geo.ready_gate = gate
        chaos_app.geo.restart()
        try:
            deadline, seen_at = await _sig_timed_out_at(chaos_app, s, inv_id)
        finally:
            gate.set()

    assert chaos_app.geo.ready_entered >= 1
    assert seen_at is not None, "SIGINT's deadline was never applied"
    assert seen_at - deadline <= timedelta(seconds=chaos_app.poll)


async def test_frozen_cancel_does_not_hold_the_tick(chaos_app) -> None:
    """Both tasks time out; GEOINT's cancel never answers. The driver keeps ticking: SIGINT is
    re-dispatched as attempt 2, while GEOINT waits for its cancel to settle before any retry (a
    late cancel by task id would otherwise stop attempt 2)."""
    chaos_app.sig.hang()
    chaos_app.geo.hang()
    chaos_app.geo.hang_cancel = True
    async with chaos_app.session() as s:
        inv_id = await s.create()
        await s.settle()
        rt = runtime(s.app)
        deadline = (await s.tasks(inv_id))[SIG].deadline
        sig_retried = False
        while chaos_app.clock.now() < deadline + timedelta(seconds=10 * chaos_app.poll):
            await _parked(chaos_app.clock, rt)
            if any(a == 2 and to == "PENDING" for a, _, to, _ in await s.changes(inv_id, SIG)):
                sig_retried = True
                break
            chaos_app.clock.advance(1.0)
        geo_changes = await s.changes(inv_id, GEO)
        geo_submits = len(chaos_app.geo.submits)
        chaos_app.geo.hang_cancel = False

    assert chaos_app.geo.cancels, "GEOINT's cancel was never sent"
    assert sig_retried, "the driver stopped ticking while GEOINT's cancel hung"
    assert not any(a == 2 for a, _, _, _ in geo_changes)
    assert geo_submits == 1
