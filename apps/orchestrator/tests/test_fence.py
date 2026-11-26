"""Fence-first terminal routine and the investigation deadline (S3b FR-123, FR-125 steps 1-4,
NFR-100, NFR-102) on a virtual clock.

``Fence.end`` is the one routine that ends an investigation from outside the normal finish path:
(1) investigation -> terminal with ``context_closed = 0``, (2) Context Service close, (3) agent
cancels, (4) open tasks -> CANCELLED. Steps 2-3 run in that order and never fail the routine.

Two ``FakeAgent`` doubles (SIGINT, GEOINT) sit behind one host-routing transport. The driver sleeps
on ``FakeClock``, so it only ticks when a test advances time; DB-backed effects are awaited with
``wait_until`` / ``pump``. The one wall-clock assertion is ``test_fence_agent_hangs_bounded``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import pytest
from orch_testkit import (
    FakeAgent,
    FakeClock,
    FakeContextService,
    Router,
    S,
    create,
    events,
    parked,
    pump,
    running,
    runtime,
    wait_until,
)

from nexus_contracts.api.states import InvestigationStatus as I
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_contracts.common import AgentId
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.main import create_app
from nexus_orchestrator.state.repository import TaskRecord
from nexus_shared.telemetry import build_tracer_provider

URLS = {"sigint-agent": "http://sigint.test", "geoint-agent": "http://geoint.test"}
STEP = 0.05  # cancel_step_timeout_seconds for the tests that really wait on it
DEADLINE = "investigation_deadline"


@pytest.fixture
def make_two(make_settings, exporter):
    def _make(sig: FakeAgent, geo: FakeAgent, ctx: FakeContextService, clock: FakeClock, **kw):
        settings = make_settings(
            nexus_enabled_agents="sigint-agent,geoint-agent",
            nexus_agent_urls=URLS,
            cancel_step_timeout_seconds=STEP,
            **kw,
        )
        router = Router({"sigint.test": sig, "geoint.test": geo})
        return create_app(
            settings,
            ctx.transport,
            agent_transport=router,
            tracer_provider=build_tracer_provider(settings, exporter),
            clock=clock,
        )

    return _make


def held(agent_id: AgentId, **kw) -> FakeAgent:
    """An agent that acknowledges and then reports WORKING forever."""
    return FakeAgent(agent_id, script=[S.WORKING], **kw)


async def by_agent(app, inv_id: str) -> dict[AgentId, TaskRecord]:
    return {t.agent_id: t for t in await runtime(app).repo.task_records(inv_id)}


def states(app, inv_id: str, want: dict[AgentId, T]) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        got = await by_agent(app, inv_id)
        return len(got) == len(want) and all(got[a].status is s for a, s in want.items())

    return check


def both_running(app, inv_id: str) -> Callable[[], Awaitable[bool]]:
    return states(app, inv_id, {AgentId.SIGINT: T.RUNNING, AgentId.GEOINT: T.RUNNING})


def investigation_terminal(app, inv_id: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        record = await runtime(app).repo.investigation_record(inv_id)
        return record is not None and record.status.is_terminal

    return check


def driver_done(app, inv_id: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return inv_id not in runtime(app).poller.active()

    return check


async def reach_deadline(app, clock: FakeClock, inv_id: str, *, seconds_before: float = 0.0):
    """Jump virtual time to ``investigation.deadline - seconds_before`` (driver parked first)."""
    record = await runtime(app).repo.investigation_record(inv_id)
    assert record is not None
    await parked(clock)
    clock.advance((record.deadline - clock.now()).total_seconds() - seconds_before)


# -- FR-123 / AC13-d ---------------------------------------------------------------------------


async def test_deadline_with_one_completed_partial(make_two, exporter) -> None:
    clock = FakeClock(utcnow())
    sig = FakeAgent(AgentId.SIGINT, script=[S.WORKING, S.COMPLETED])
    geo = held(AgentId.GEOINT)
    ctx = FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(
            clock, states(app, inv_id, {AgentId.SIGINT: T.COMPLETED, AgentId.GEOINT: T.RUNNING})
        )
        geo_task = (await by_agent(app, inv_id))[AgentId.GEOINT]

        await reach_deadline(app, clock, inv_id)
        await wait_until(driver_done(app, inv_id))
        body = (await client.get(f"/api/v1/investigations/{inv_id}")).json()
        tasks = await by_agent(app, inv_id)
        record = await runtime(app).repo.investigation_record(inv_id)
        changed = await events(client, inv_id, "task.status_changed")

    assert body["status"] == "PARTIAL" and body["status_reason"] == DEADLINE
    assert tasks[AgentId.SIGINT].status is T.COMPLETED
    assert tasks[AgentId.GEOINT].status is T.CANCELLED
    assert tasks[AgentId.GEOINT].status_reason == DEADLINE
    assert ctx.closes == [(inv_id, "PARTIAL")]
    assert geo.cancels == [geo_task.id] and sig.cancels == []  # the finished task is left alone
    assert record is not None and record.context_closed is True
    [cancelled] = [e.payload for e in changed if e.payload["to_state"] == "CANCELLED"]
    assert cancelled["task_id"] == geo_task.id and cancelled["reason"] == DEADLINE
    spans = [s for s in exporter.get_finished_spans() if s.name == "workflow.deadline"]
    assert len(spans) == 1
    assert spans[0].attributes["nexus.investigation.id"] == inv_id
    assert spans[0].attributes["nexus.investigation.status"] == "PARTIAL"


async def test_deadline_with_none_completed_failed(make_two) -> None:
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))

        await reach_deadline(app, clock, inv_id)
        await wait_until(driver_done(app, inv_id))
        body = (await client.get(f"/api/v1/investigations/{inv_id}")).json()
        tasks = await by_agent(app, inv_id)

    assert body["status"] == "FAILED" and body["status_reason"] == DEADLINE
    assert {t.status for t in tasks.values()} == {T.CANCELLED}
    assert {t.status_reason for t in tasks.values()} == {DEADLINE}
    assert ctx.closes == [(inv_id, "FAILED")]
    assert len(sig.cancels) == 1 and len(geo.cancels) == 1


async def test_deadline_is_not_early(make_two) -> None:
    """One second before ``investigation.deadline`` nothing is fenced."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock, task_deadline_seconds=900)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        await reach_deadline(app, clock, inv_id, seconds_before=1.0)
        await parked(clock)
        record = await runtime(app).repo.investigation_record(inv_id)

    assert record is not None and record.status is I.RUNNING
    assert ctx.closes == [] and sig.cancels == [] and geo.cancels == []


# -- FR-125 order and bounds -------------------------------------------------------------------


async def test_fence_order_close_before_agent_cancel(make_two) -> None:
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    journal: list[str] = []
    sig.journal = geo.journal = ctx.journal = journal
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        ids = sorted(f"cancel:{t.id}" for t in (await by_agent(app, inv_id)).values())

        await reach_deadline(app, clock, inv_id)
        await wait_until(driver_done(app, inv_id))

    assert journal[0] == "close"
    assert sorted(journal[1:]) == ids  # every cancel, all after the close


@pytest.mark.parametrize("failure", ["hangs", "down"])
async def test_fence_cs_down_context_closed_0(make_two, failure: str) -> None:
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        if failure == "hangs":
            ctx.close_hangs = True
        else:
            ctx.up = False

        await reach_deadline(app, clock, inv_id)
        await wait_until(driver_done(app, inv_id))
        body = (await client.get(f"/api/v1/investigations/{inv_id}")).json()
        tasks = await by_agent(app, inv_id)
        record = await runtime(app).repo.investigation_record(inv_id)

    assert body["status"] == "FAILED" and body["status_reason"] == DEADLINE
    assert record is not None and record.context_closed is False
    assert len(sig.cancels) == 1 and len(geo.cancels) == 1  # step 3 ran regardless of step 2
    assert {t.status for t in tasks.values()} == {T.CANCELLED}


async def test_fence_agent_hangs_bounded(make_two) -> None:
    """Wall time on purpose: a cancel that never returns costs one step timeout, not forever."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        sig.hang_cancel = geo.hang_cancel = True

        started = time.monotonic()
        result = await runtime(app).fence.end(inv_id, I.FAILED, DEADLINE, task_reason=DEADLINE)
        elapsed = time.monotonic() - started
        tasks = await by_agent(app, inv_id)

    assert elapsed < 2 * STEP + 0.1, elapsed
    assert result.applied and result.status is I.FAILED and result.context_closed is True
    assert {t.status for t in tasks.values()} == {T.CANCELLED}  # step 4 ran after the timeout


async def test_completion_racing_fence(make_two, caplog) -> None:
    """Review Focus 2: a task completes between steps 1 and 4. Its CANCELLED write is DROPPED."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        rt = runtime(app)
        stale = (await by_agent(app, inv_id))[AgentId.SIGINT]

        async def complete_then_ok(agent: AgentId, task_id: str) -> bool:
            if agent is AgentId.SIGINT:
                async with rt.db.transaction() as tx:
                    await rt.repo.transition_task(tx, stale, T.COMPLETED, now=utcnow())
            return True

        rt.agents.cancel = complete_then_ok  # type: ignore[method-assign]
        result = await rt.fence.end(inv_id, I.FAILED, DEADLINE, task_reason=DEADLINE)
        tasks = await by_agent(app, inv_id)
        [note] = [e.payload for e in await events(client, inv_id, "transition.dropped")]

    assert result.applied
    assert tasks[AgentId.SIGINT].status is T.COMPLETED  # the completion won
    assert tasks[AgentId.GEOINT].status is T.CANCELLED
    assert note["entity"] == "task" and note["current_state"] == "COMPLETED"
    assert note["attempted_state"] == "CANCELLED" and note["reason"] == DEADLINE


async def test_fence_is_a_noop_on_a_terminal_investigation(make_two) -> None:
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        fence = runtime(app).fence

        first = await fence.end(inv_id, I.CANCELLED, None, task_reason=None)
        second = await fence.end(inv_id, I.FAILED, DEADLINE, task_reason=DEADLINE)
        again = await fence.end(inv_id, I.CANCELLED, None, task_reason=None)
        tasks = await by_agent(app, inv_id)

    assert first.applied and first.status is I.CANCELLED and first.context_closed is True
    assert not second.applied and second.status is I.CANCELLED and second.context_closed is True
    assert not again.applied and again.status is I.CANCELLED  # same status: NOOP, not applied
    assert ctx.closes == [(inv_id, "CANCELLED")]  # the later calls did nothing
    assert len(sig.cancels) == 1 and len(geo.cancels) == 1
    assert {t.status for t in tasks.values()} == {T.CANCELLED}
    assert {t.status_reason for t in tasks.values()} == {None}


# -- NFR-100 -----------------------------------------------------------------------------------


async def test_no_investigation_stays_running_past_deadline(make_two) -> None:
    """Neither agent ever answers. The task deadline is lifted to the investigation deadline, so
    the fence (not a task timeout) is what ends it, one tick after 600 s."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    for agent in (sig, geo):
        agent.hold = True  # acknowledged, never progresses
        agent.ready = False
    app = make_two(sig, geo, ctx, clock, task_deadline_seconds=900)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(
            states(app, inv_id, {AgentId.SIGINT: T.DISPATCHED, AgentId.GEOINT: T.DISPATCHED})
        )
        await reach_deadline(app, clock, inv_id, seconds_before=1.0)
        await parked(clock)
        before = await runtime(app).repo.investigation_record(inv_id)
        assert before is not None and before.status is I.RUNNING

        clock.advance(1.0)
        await wait_until(investigation_terminal(app, inv_id))
        body = (await client.get(f"/api/v1/investigations/{inv_id}")).json()

    assert body["status"] == "FAILED"  # never RUNNING past the deadline
    assert body["status_reason"] == DEADLINE
