"""Investigation cancel endpoint (S3b FR-125, NFR-102): ``POST /api/v1/investigations/{id}/cancel``.

The route is a thin wrapper over ``Fence.end(id, CANCELLED, None, task_reason=None)`` (the fence's
own order and bounds are covered by ``test_fence.py``). What is checked here is the HTTP contract
(200 / 404 / 409), that the driver stops, that the creation retrier drops a cancelled CREATED
investigation, and the one race that matters: cancel against the last task completing.

Two ``FakeAgent`` doubles sit behind one host-routing transport. The driver sleeps on
``FakeClock``. The one real-time assertion is ``test_cancel_within_budget_when_everything_hangs``.
"""

from __future__ import annotations

import asyncio
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
    pump,
    running,
    runtime,
    wait_until,
)

from nexus_contracts.api.problem import ProblemDetails
from nexus_contracts.api.states import InvestigationStatus as I
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_contracts.common import AgentId
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.main import create_app
from nexus_orchestrator.state.repository import TaskRecord
from nexus_shared.telemetry import build_tracer_provider

URLS = {"sigint-agent": "http://sigint.test", "geoint-agent": "http://geoint.test"}
TERMINAL = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}


@pytest.fixture
def make_two(make_settings, exporter):
    def _make(sig: FakeAgent, geo: FakeAgent, ctx: FakeContextService, clock: FakeClock, **kw):
        kw.setdefault("cancel_step_timeout_seconds", 0.05)
        settings = make_settings(
            nexus_enabled_agents="sigint-agent,geoint-agent", nexus_agent_urls=URLS, **kw
        )
        return create_app(
            settings,
            ctx.transport,
            agent_transport=Router({"sigint.test": sig, "geoint.test": geo}),
            tracer_provider=build_tracer_provider(settings, exporter),
            clock=clock,
        )

    return _make


def held(agent_id: AgentId) -> FakeAgent:
    """An agent that acknowledges and then reports WORKING forever."""
    return FakeAgent(agent_id, script=[S.WORKING])


async def by_agent(app, inv_id: str) -> dict[AgentId, TaskRecord]:
    return {t.agent_id: t for t in await runtime(app).repo.task_records(inv_id)}


def both_running(app, inv_id: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        got = await by_agent(app, inv_id)
        return len(got) == 2 and all(t.status is T.RUNNING for t in got.values())

    return check


def cancel_url(inv_id: str) -> str:
    return f"/api/v1/investigations/{inv_id}/cancel"


async def terminal_events(client, inv_id: str) -> list[dict]:
    return [
        e.payload
        for e in await events(client, inv_id, "investigation.status_changed")
        if e.payload["to_status"] in TERMINAL
    ]


# -- 200 ----------------------------------------------------------------------------------------


async def test_cancel_running_returns_cancelled_context_closed_true(make_two, exporter) -> None:
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        task_ids = {t.id for t in (await by_agent(app, inv_id)).values()}

        resp = await client.post(cancel_url(inv_id))
        tasks = await by_agent(app, inv_id)
        record = await runtime(app).repo.investigation_record(inv_id)
        body = (await client.get(f"/api/v1/investigations/{inv_id}")).json()
        ended = await terminal_events(client, inv_id)
        await pump(clock, _driver_gone(app, inv_id))  # no stop call: it exits at its next tick

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": inv_id, "status": "CANCELLED", "context_closed": True}
    assert body["status"] == "CANCELLED" and body["status_reason"] is None
    assert record is not None and record.context_closed is True
    assert {t.status for t in tasks.values()} == {T.CANCELLED}
    assert {t.status_reason for t in tasks.values()} == {None}  # CANCELLED is the reason
    assert set(sig.cancels + geo.cancels) == task_ids
    assert len(sig.cancels) == 1 and len(geo.cancels) == 1
    assert ctx.closes == [(inv_id, "CANCELLED")]
    assert len(ended) == 1 and ended[0]["to_status"] == "CANCELLED"
    spans = [s for s in exporter.get_finished_spans() if s.name == "workflow.cancel"]
    assert len(spans) == 1
    assert spans[0].attributes["nexus.investigation.id"] == inv_id
    fence = [s for s in exporter.get_finished_spans() if s.name == "workflow.fence"]
    assert len(fence) == 1 and fence[0].parent.span_id == spans[0].context.span_id


@pytest.mark.parametrize("failure", ["down", "hangs"])
async def test_cancel_cs_down_context_closed_false(make_two, failure: str) -> None:
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        if failure == "down":
            ctx.up = False
        else:
            ctx.close_hangs = True

        resp = await client.post(cancel_url(inv_id))
        tasks = await by_agent(app, inv_id)
        record = await runtime(app).repo.investigation_record(inv_id)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": inv_id, "status": "CANCELLED", "context_closed": False}
    assert record is not None and record.context_closed is False  # left for the reconciler
    assert len(sig.cancels) == 1 and len(geo.cancels) == 1  # step 3 ran regardless of step 2
    assert {t.status for t in tasks.values()} == {T.CANCELLED}


@pytest.mark.slow
async def test_cancel_within_budget_when_everything_hangs(make_two) -> None:
    """NFR-102, the only real-time test: CS close and both agent cancels never answer, each step
    timeout is 1.0 s, and the response still arrives in under 2.5 s."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock, cancel_step_timeout_seconds=1.0)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        ctx.close_hangs = sig.hang_cancel = geo.hang_cancel = True

        started = time.monotonic()
        resp = await client.post(cancel_url(inv_id))
        elapsed = time.monotonic() - started
        tasks = await by_agent(app, inv_id)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": inv_id, "status": "CANCELLED", "context_closed": False}
    assert elapsed < 2.5, elapsed
    assert elapsed >= 1.0  # it really waited on the hung steps
    assert {t.status for t in tasks.values()} == {T.CANCELLED}


async def test_cancel_created_investigation(make_two) -> None:
    """CREATED (Context Service registration pending): cancel works, and neither the retrier nor
    a late ``begin`` can start it afterwards."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService(up=False)
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        rt = runtime(app)
        record = await rt.repo.investigation_record(inv_id)
        assert record is not None and record.status is I.CREATED
        assert inv_id in rt.workflow.pending_registration

        resp = await client.post(cancel_url(inv_id))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": inv_id, "status": "CANCELLED", "context_closed": False}

        # The Context Service comes back and the retrier runs: it must drop the id, not register
        # or start it.
        ctx.up = True
        await wait_until(_dropped(app, inv_id))
        await asyncio.sleep(0.2)  # several more retrier passes
        manifest = await rt.catalog.get(record.scenario_id)
        assert manifest is not None
        await rt.workflow.begin(inv_id, manifest)  # a racing start path
        after = await rt.repo.investigation_record(inv_id)
        tasks = await by_agent(app, inv_id)

    assert after is not None and after.status is I.CANCELLED
    assert tasks == {}
    assert ctx.creates() == []  # never registered
    assert sig.submits == [] and geo.submits == []
    assert inv_id not in rt.poller.active()


def _dropped(app, inv_id: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return inv_id not in runtime(app).workflow.pending_registration

    return check


# -- 404 / 409 ----------------------------------------------------------------------------------


async def test_cancel_unknown_404(make_two) -> None:
    clock = FakeClock(utcnow())
    app = make_two(held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService(), clock)
    async with running(app) as client:
        resp = await client.post(cancel_url("inv_doesnotexist"))
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "inv_doesnotexist" in ProblemDetails.model_validate(resp.json()).detail


@pytest.mark.parametrize("ended_as", ["CANCELLED", "FAILED"])
async def test_cancel_terminal_409_investigation_terminal(make_two, ended_as: str) -> None:
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        if ended_as == "CANCELLED":
            assert (await client.post(cancel_url(inv_id))).status_code == 200
        else:
            fence = runtime(app).fence
            fenced = await fence.end(inv_id, I.FAILED, "investigation_deadline", task_reason=None)
            assert fenced.applied
        timeline = f"/api/v1/investigations/{inv_id}/timeline"
        before = len((await client.get(timeline)).json()["items"])

        resp = await client.post(cancel_url(inv_id))
        after = len((await client.get(timeline)).json()["items"])

    assert resp.status_code == 409, resp.text
    problem = ProblemDetails.model_validate(resp.json())
    assert problem.code == "investigation_terminal"
    assert problem.detail == f"investigation is {ended_as}"
    assert after == before  # a refused cancel leaves no trace on the timeline
    assert ctx.closes == [(inv_id, ended_as)] and len(sig.cancels) == 1  # nothing ran twice


async def test_cancel_409_when_fence_loses_the_race(make_two) -> None:
    """The route saw RUNNING, but the investigation ended before the fence's guarded write:
    ``applied=False`` is the same 409."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        rt = runtime(app)
        real_end = rt.fence.end

        async def end_after_someone_else(*args, **kwargs):
            await real_end(inv_id, I.FAILED, "investigation_deadline", task_reason=None)
            return await real_end(*args, **kwargs)

        rt.fence.end = end_after_someone_else  # type: ignore[method-assign]
        resp = await client.post(cancel_url(inv_id))

    assert resp.status_code == 409, resp.text
    problem = ProblemDetails.model_validate(resp.json())
    assert problem.code == "investigation_terminal" and problem.detail == "investigation is FAILED"


# -- race ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("lead", [0.0, 0.004, 0.012])
async def test_cancel_vs_completion_race(make_two, lead: float) -> None:
    """The poll completes the last tasks while the cancel is in flight. Whoever wins, there is no
    500, the status is CANCELLED or PARTIAL, and the timeline has exactly one terminal event."""
    clock = FakeClock(utcnow())
    sig, geo = held(AgentId.SIGINT), held(AgentId.GEOINT)
    ctx = FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        sig.script = geo.script = [S.WORKING, S.COMPLETED]  # the next poll completes both

        async def cancel() -> int:
            await asyncio.sleep(lead)
            return (await client.post(cancel_url(inv_id))).status_code

        async def tick() -> None:
            for _ in range(10):
                clock.advance(0.02)
                await asyncio.sleep(0.004)

        code, _ = await asyncio.gather(cancel(), tick())
        await wait_until(_ended(app, inv_id))
        record = await runtime(app).repo.investigation_record(inv_id)
        ended = await terminal_events(client, inv_id)
        tasks = await by_agent(app, inv_id)

    assert record is not None and record.status in (I.CANCELLED, I.PARTIAL)
    assert code == (200 if record.status is I.CANCELLED else 409)  # never a 500
    assert len(ended) == 1 and ended[0]["to_status"] == record.status.value
    assert all(t.status.is_terminal for t in tasks.values())
    if record.status is I.PARTIAL:  # completion won: its tasks were not cancelled
        assert {t.status for t in tasks.values()} == {T.COMPLETED}


def _ended(app, inv_id: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        record = await runtime(app).repo.investigation_record(inv_id)
        return record is not None and record.status.is_terminal

    return check


# -- review fixes: in-flight work that lands after the fence -------------------------------------


async def test_submit_in_flight_during_cancel_is_cancelled_at_the_agent(make_two) -> None:
    """I1: the dispatch POST is in flight when the fence runs. The fence's agent cancel reaches
    the agent first (it does not know the task yet), then the submit lands. The dispatcher's ack
    write is dropped (row already CANCELLED), so it must cancel the agent side itself."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    sig.submit_gate = asyncio.Event()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(_entered(sig))
        rt = runtime(app)
        task_id = (await by_agent(app, inv_id))[AgentId.SIGINT].id

        result = await rt.fence.end(inv_id, I.CANCELLED, None, task_reason=None)
        assert result.applied
        assert sig.log == [f"cancel:{task_id}"]  # the submit has not landed yet

        sig.submit_gate.set()
        await wait_until(_log_len(sig, 3))
        row = (await by_agent(app, inv_id))[AgentId.SIGINT]

    assert sig.log == [f"cancel:{task_id}", f"submit:{task_id}", f"cancel:{task_id}"]
    assert row.status is T.CANCELLED


async def test_submit_in_flight_during_route_cancel_is_cancelled_at_the_agent(make_two) -> None:
    """I1 through the real route. The route does not stop the driver, so the in-flight submit
    completes, its ack write is dropped (row CANCELLED) and the agent side is cancelled."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    sig.submit_gate = asyncio.Event()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(_entered(sig))
        task_id = (await by_agent(app, inv_id))[AgentId.SIGINT].id

        resp = await client.post(cancel_url(inv_id))
        assert resp.status_code == 200, resp.text
        assert sig.log == [f"cancel:{task_id}"]  # the submit has not landed yet

        sig.submit_gate.set()
        await wait_until(_log_len(sig, 3))
        await pump(clock, _driver_gone(app, inv_id))
        row = (await by_agent(app, inv_id))[AgentId.SIGINT]

    assert sig.log == [f"cancel:{task_id}", f"submit:{task_id}", f"cancel:{task_id}"]
    assert row.status is T.CANCELLED


async def test_driver_exits_at_its_next_tick_after_cancel(make_two) -> None:
    """No stop call: the driver sees a non-RUNNING investigation and leaves on its own."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, both_running(app, inv_id))
        assert inv_id in runtime(app).poller.active()

        assert (await client.post(cancel_url(inv_id))).status_code == 200
        await pump(clock, _driver_gone(app, inv_id))

    assert inv_id not in runtime(app).poller.active()


def _driver_gone(app, inv_id: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return inv_id not in runtime(app).poller.active()

    return check


def _entered(agent: FakeAgent) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return agent.submit_entered >= 1

    return check


def _log_len(agent: FakeAgent, n: int) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return len(agent.log) >= n

    return check


async def test_register_in_flight_during_cancel_is_closed_afterwards(make_two) -> None:
    """I2: the Context Service create is in flight when the cancel lands (the fence's close sees
    nothing registered yet and counts it as closed). When the create then lands, the record would
    stay open forever (the reconciler only selects context_closed = 0), so the registration path
    closes it again, and the investigation is not started."""
    clock = FakeClock(utcnow())
    sig, geo, ctx = held(AgentId.SIGINT), held(AgentId.GEOINT), FakeContextService()
    ctx.create_gate = asyncio.Event()
    app = make_two(sig, geo, ctx, clock)
    async with running(app) as client:
        rt = runtime(app)
        creating = asyncio.create_task(create(client))
        await wait_until(_create_entered(ctx))
        [inv_id] = await rt.repo.list_ids_with_status(I.CREATED)

        resp = await client.post(cancel_url(inv_id))
        assert resp.status_code == 200, resp.text
        assert ctx.log == [f"close:{inv_id}"]  # the create has not landed yet

        ctx.create_gate.set()
        assert await creating == inv_id
        await wait_until(_ctx_log_len(ctx, 3))
        record = await rt.repo.investigation_record(inv_id)
        tasks = await by_agent(app, inv_id)

    assert ctx.log == [f"close:{inv_id}", f"create:{inv_id}", f"close:{inv_id}"]
    assert ctx.closes == [(inv_id, "CANCELLED"), (inv_id, "CANCELLED")]
    assert record is not None and record.status is I.CANCELLED
    assert tasks == {} and sig.submits == []  # never started


def _create_entered(ctx: FakeContextService) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return ctx.create_entered >= 1

    return check


def _ctx_log_len(ctx: FakeContextService, n: int) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return len(ctx.log) >= n

    return check
