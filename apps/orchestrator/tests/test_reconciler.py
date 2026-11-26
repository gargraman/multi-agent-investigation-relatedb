"""Close reconciler (S3b FR-126): terminal investigations with ``context_closed = 0`` get their
Context Service close at startup and every ``reconcile_interval_seconds``, leader only.

The reconciler sleeps on ``FakeClock``; a test makes the next pass happen with ``advance(30)``.
Investigations are created through the API, then ended directly in the repository (the normal
``_maybe_finish`` end leaves ``context_closed = 0``, which is exactly the state to reconcile).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from orch_testkit import FakeClock, FakeContextService, create, running, runtime, wait_until

from nexus_contracts.api.states import InvestigationStatus as I
from nexus_orchestrator.ids import utcnow


async def end_as(app, inv_id: str, status: I) -> None:
    rt = runtime(app)
    async with rt.db.transaction() as tx:
        await rt.repo.transition_investigation(tx, inv_id, status, now=utcnow())


def closed_flag(app, inv_id: str, expected: bool) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        record = await runtime(app).repo.investigation_record(inv_id)
        return record is not None and record.context_closed is expected

    return check


async def reconciler_parked(clock: FakeClock) -> None:
    """The startup pass is done and the loop is sleeping on the clock (nothing else sleeps yet)."""

    async def sleeping() -> bool:
        return clock.pending() >= 1

    await wait_until(sleeping)


async def test_run_once_closes_unclosed_terminal(make_app) -> None:
    ctx, clock = FakeContextService(), FakeClock(utcnow())
    app = make_app(ctx, clock=clock)
    async with running(app) as client:
        await reconciler_parked(clock)
        inv_id = await create(client)
        await end_as(app, inv_id, I.PARTIAL)
        assert (await runtime(app).repo.investigation_record(inv_id)).context_closed is False

        assert await runtime(app).reconciler.run_once() == 1

        record = await runtime(app).repo.investigation_record(inv_id)
    assert record is not None and record.context_closed is True
    assert ctx.closes == [(inv_id, "PARTIAL")]  # the body carries the investigation's status


async def test_run_once_skips_non_terminal_and_closed(make_app) -> None:
    ctx, clock = FakeContextService(), FakeClock(utcnow())
    app = make_app(ctx, clock=clock)
    async with running(app) as client:
        await reconciler_parked(clock)
        running_id = await create(client)  # RUNNING, held at SUBMITTED
        closed_id = await create(client)
        await end_as(app, closed_id, I.FAILED)
        rt = runtime(app)
        async with rt.db.transaction() as tx:
            await rt.repo.set_context_closed(tx, closed_id, True, utcnow())

        assert await rt.reconciler.run_once() == 0

        running_record = await rt.repo.investigation_record(running_id)
    assert ctx.closes == []
    assert running_record is not None and running_record.status is I.RUNNING


async def test_cs_down_then_up_closes_next_pass(make_app) -> None:
    """AC13-c in-process: a Context Service outage leaves 0; the pass 30 s later closes it."""
    ctx, clock = FakeContextService(), FakeClock(utcnow())
    app = make_app(ctx, clock=clock)
    async with running(app) as client:
        await reconciler_parked(clock)
        inv_id = await create(client)
        await end_as(app, inv_id, I.PARTIAL)
        ctx.up = False

        assert await runtime(app).reconciler.run_once() == 0
        assert (await runtime(app).repo.investigation_record(inv_id)).context_closed is False

        ctx.up = True
        clock.advance(30)  # wakes the loop: next pass
        await wait_until(closed_flag(app, inv_id, True))
    assert ctx.closes == [(inv_id, "PARTIAL")]


async def test_not_leader_skips(make_app) -> None:
    ctx, clock = FakeContextService(), FakeClock(utcnow())
    app = make_app(ctx, clock=clock)
    async with running(app) as client:
        await reconciler_parked(clock)
        inv_id = await create(client)
        await end_as(app, inv_id, I.PARTIAL)
        rt = runtime(app)
        rt.reconciler.is_leader = lambda: False

        assert await rt.reconciler.run_once() == 0

        record = await rt.repo.investigation_record(inv_id)
    assert ctx.closes == []
    assert record is not None and record.context_closed is False


async def test_runs_at_startup(make_app) -> None:
    """A restart finds the unclosed terminal investigation and closes it before any clock tick."""
    first_ctx, clock = FakeContextService(), FakeClock(utcnow())
    first = make_app(first_ctx, clock=clock)
    async with running(first) as client:
        await reconciler_parked(clock)
        inv_id = await create(client)
        await end_as(first, inv_id, I.FAILED)
    assert first_ctx.closes == []  # stopped before any pass saw it

    ctx = FakeContextService()
    second = make_app(ctx, clock=FakeClock(utcnow()))  # no advance: only the startup pass runs
    async with running(second):
        await wait_until(closed_flag(second, inv_id, True))
    assert ctx.closes == [(inv_id, "FAILED")]


async def test_one_failure_does_not_stop_the_pass(make_app, exporter, monkeypatch) -> None:
    """Sequential closes: the first raises, the second is still closed; each gets its own span."""
    ctx, clock = FakeContextService(), FakeClock(utcnow())
    app = make_app(ctx, clock=clock)
    async with running(app) as client:
        await reconciler_parked(clock)
        first, second = await create(client), await create(client)
        await end_as(app, first, I.PARTIAL)
        await end_as(app, second, I.FAILED)
        rt = runtime(app)
        real_close = rt.context.close

        async def flaky(investigation_id, status, *, timeout):
            if investigation_id == first:
                raise RuntimeError("boom")
            return await real_close(investigation_id, status, timeout=timeout)

        monkeypatch.setattr(rt.context, "close", flaky)

        assert await rt.reconciler.run_once() == 1

        first_record = await rt.repo.investigation_record(first)
        second_record = await rt.repo.investigation_record(second)
    assert first_record is not None and first_record.context_closed is False
    assert second_record is not None and second_record.context_closed is True
    spans = [s for s in exporter.get_finished_spans() if s.name == "workflow.reconcile_close"]
    assert sorted(s.attributes["nexus.investigation.id"] for s in spans) == sorted([first, second])


async def test_hung_close_times_out_and_stays_unclosed(make_app) -> None:
    ctx, clock = FakeContextService(), FakeClock(utcnow())
    ctx.close_hangs = True
    app = make_app(ctx, clock=clock, context_service_timeout_seconds=0.05)
    async with running(app) as client:
        await reconciler_parked(clock)
        inv_id = await create(client)
        await end_as(app, inv_id, I.PARTIAL)

        assert await runtime(app).reconciler.run_once() == 0

        record = await runtime(app).repo.investigation_record(inv_id)
    assert record is not None and record.context_closed is False
