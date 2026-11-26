"""Single re-dispatch with a readiness wait window (S3b FR-121, FR-121a, FR-122) on a virtual clock.

A task that timed out (or failed with ``agent_restarted`` / ``task_lost``) at attempt 1 goes back
to PENDING as attempt 2, but only while the agent's ``/readyz`` is green and only until the wait
window closes. The investigation is held open while the window is open (FR-121a).

The driver sleeps on ``FakeClock``; effects are DB-backed, so tests wait on real conditions
(``wait_until`` / ``pump`` / ``parked``). ``FakeAgent`` is a double at the HTTP boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import pytest
from orch_testkit import (
    FakeAgent,
    FakeClock,
    FakeContextService,
    S,
    create,
    events,
    parked,
    pump,
    running,
    runtime,
    task_in,
    the_task,
    wait_until,
)

from nexus_contracts.a2a.task import TaskResult
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.repository import TaskRecord
from nexus_orchestrator.workflow.deadlines import (
    attempt2_reservation,
    retry_eligible,
    retry_window_end,
    retry_window_open,
)

TASK_DEADLINE = 180.0
POLL = 1.0  # one driver tick per virtual second keeps "a tick" explicit in the window tests


def submits(agent: FakeAgent, n: int) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return len(agent.submits) >= n

    return check


def driver_done(app, inv_id: str) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return inv_id not in runtime(app).poller.active()

    return check


async def detail(client, inv_id: str) -> dict:
    return (await client.get(f"/api/v1/investigations/{inv_id}")).json()


async def time_out_first_attempt(app, client, clock: FakeClock) -> tuple[str, TaskRecord]:
    """Create an investigation, let attempt 1 run, then pass its deadline. Returns the id and the
    task as TIMED_OUT (or already re-dispatched if the agent is ready: callers decide)."""
    inv_id = await create(client)
    await wait_until(task_in(app, inv_id, T.RUNNING))
    await parked(clock)
    first = await the_task(app, inv_id)
    clock.advance(TASK_DEADLINE)
    return inv_id, first


# -- the pure helpers -------------------------------------------------------------------------

T0 = datetime(2026, 1, 1, 12, 0, 0).astimezone()


def _task(
    status: T = T.TIMED_OUT,
    reason: str | None = None,
    attempt: int = 1,
    usage: int | None = None,
    reservation: int = 15000,
) -> TaskRecord:
    return TaskRecord(
        id="tsk_x",
        investigation_id="inv_x",
        agent_id=FakeAgent().agent_id,
        task_type="t",
        status=status,
        status_reason=reason,
        attempt=attempt,
        deadline=T0,
        token_reservation=reservation,
        acked_at=None,
        version=1,
        updated_at=T0,
        usage_tokens=usage,
    )


@pytest.mark.parametrize(
    ("task", "eligible"),
    [
        (_task(T.TIMED_OUT, "deadline_exceeded"), True),
        (_task(T.FAILED, "agent_restarted"), True),
        (_task(T.FAILED, "task_lost"), True),
        (_task(T.FAILED, "internal_error"), False),
        (_task(T.FAILED, "task_rejected"), False),
        (_task(T.FAILED, None), False),
        (_task(T.FAILED, "not-a-code"), False),
        (_task(T.COMPLETED), False),
        (_task(T.CANCELLED), False),
        (_task(T.RUNNING), False),
        (_task(T.TIMED_OUT, "deadline_exceeded", attempt=2), False),  # FR-122
        (_task(T.FAILED, "task_lost", attempt=2), False),
    ],
)
def test_retry_eligible(task: TaskRecord, eligible: bool) -> None:
    assert retry_eligible(task) is eligible


def test_attempt2_reservation_is_share_of_cap_minus_usage() -> None:
    assert attempt2_reservation(0.25, 60000, None) == 15000  # unknown usage counts as 0
    assert attempt2_reservation(0.25, 60000, 4000) == 11000
    assert attempt2_reservation(0.25, 60000, 15000) == 0
    assert attempt2_reservation(0.25, 60000, 20000) == -5000  # callers treat <= 0 as "no retry"
    assert attempt2_reservation(0.25, 60001, 0) == 15000  # floored


def test_window_end_and_open(make_settings) -> None:
    s: OrchestratorSettings = make_settings()
    far = T0 + timedelta(hours=1)
    task = _task()
    assert retry_window_end(task, far, s) == T0 + timedelta(seconds=180)
    assert retry_window_end(task, T0 + timedelta(seconds=60), s) == T0 + timedelta(seconds=60)
    just_before = T0 + timedelta(seconds=179.9)
    assert retry_window_open(task, far, just_before, s)
    assert not retry_window_open(task, far, T0 + timedelta(seconds=180), s)  # end is exclusive
    assert not retry_window_open(task, T0 + timedelta(seconds=60), T0 + timedelta(seconds=60), s)
    assert not retry_window_open(_task(T.COMPLETED), far, T0, s)  # not eligible: never open
    assert not retry_window_open(_task(attempt=2), far, T0, s)


def test_window_is_closed_when_no_reservation_is_left(make_settings) -> None:
    s = make_settings()
    far = T0 + timedelta(hours=1)
    spent = _task(usage=15000, reservation=15000)
    assert not retry_window_open(spent, far, T0, s)
    assert retry_window_open(_task(usage=14999, reservation=15000), far, T0, s)


# -- FR-121 -----------------------------------------------------------------------------------


async def test_timed_out_redispatched_when_ready(make_app, exporter) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])  # attempt 1 stays RUNNING until its deadline
    app = make_app(FakeContextService(), agent, clock=clock, poll_interval_seconds=POLL)
    async with running(app) as client:
        inv_id, first = await time_out_first_attempt(app, client, clock)
        await wait_until(submits(agent, 2))
        await wait_until(task_in(app, inv_id, T.DISPATCHED, T.RUNNING))
        task = await the_task(app, inv_id)
        changed = [e.payload for e in await events(client, inv_id, "task.status_changed")]
        redispatch_time = clock.now()

    assert task.attempt == 2 and first.attempt == 1
    assert task.deadline == redispatch_time + timedelta(seconds=180)
    assert task.token_reservation == 15000
    _, body, _ = agent.submits[1]
    assert body.attempt == 2 and body.token_budget == 15000 and body.deadline == task.deadline
    assert agent.statuses() == [202, 202]
    [pending] = [
        p for p in changed if p["from_state"] == "TIMED_OUT" and p["to_state"] == "PENDING"
    ]
    assert pending["attempt"] == 2
    assert pending["task_id"] == first.id
    spans = [s for s in exporter.get_finished_spans() if s.name == "workflow.redispatch"]
    assert len(spans) == 1 and spans[0].attributes["nexus.task.id"] == first.id


async def test_redispatch_clears_the_stale_failure_of_attempt1(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING, S.FAILED], fail_code=ErrorCode.AGENT_RESTARTED)
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, task_in(app, inv_id, T.FAILED))
        agent.script = [S.WORKING]  # attempt 2 will now hang, so its row stays in view
        await parked(clock)
        await pump(clock, submits(agent, 2))
        await wait_until(task_in(app, inv_id, T.DISPATCHED, T.RUNNING))
        view = await detail(client, inv_id)

    [t] = view["tasks"]
    assert t["attempt"] == 2 and t["error_code"] is None and t["error_category"] is None


async def test_reservation_reduced_by_attempt1_usage(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(
        script=[S.WORKING, S.FAILED], fail_code=ErrorCode.AGENT_RESTARTED, usage=(3000, 1000)
    )
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, submits(agent, 2))
        task = await the_task(app, inv_id)
        first = [p for p in await events(client, inv_id, "task.status_changed")]

    assert task.attempt == 2 and task.token_reservation == 11000  # 15000 - 4000
    assert agent.submits[1][1].token_budget == 11000
    assert first  # the transition history is intact


async def test_not_ready_waits_then_final(make_app) -> None:
    """FR-121a: while the window is open the investigation stays RUNNING; at 180 s it closes."""
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])
    app = make_app(FakeContextService(), agent, clock=clock, poll_interval_seconds=POLL)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.RUNNING))
        await parked(clock)
        agent.ready = False
        clock.advance(TASK_DEADLINE)
        await wait_until(task_in(app, inv_id, T.TIMED_OUT))
        await parked(clock)

        clock.advance(179)
        await parked(clock)
        mid = await detail(client, inv_id)
        hits = agent.readyz_hits
        assert (await the_task(app, inv_id)).status is T.TIMED_OUT
        assert mid["status"] == "RUNNING"  # held open by the retry window

        clock.advance(1)  # the window closes
        await wait_until(driver_done(app, inv_id))
        end = await detail(client, inv_id)
        task = await the_task(app, inv_id)

    assert hits >= 1  # readiness was actually asked inside the window
    assert task.status is T.TIMED_OUT and task.attempt == 1
    assert end["status"] == "FAILED" and end["status_reason"] == "deadline_exceeded"
    assert len(agent.submits) == 1


async def test_ready_mid_window_redispatches(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])
    app = make_app(FakeContextService(), agent, clock=clock, poll_interval_seconds=POLL)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.RUNNING))
        await parked(clock)
        agent.ready = False
        clock.advance(TASK_DEADLINE)
        await wait_until(task_in(app, inv_id, T.TIMED_OUT))
        await parked(clock)

        for _ in range(2):  # 20 s of "not ready"
            clock.advance(10)
            await parked(clock)
        assert len(agent.submits) == 1
        assert (await the_task(app, inv_id)).status is T.TIMED_OUT

        agent.ready = True
        clock.advance(10)
        await wait_until(submits(agent, 2))
        await wait_until(task_in(app, inv_id, T.DISPATCHED, T.RUNNING))
        task = await the_task(app, inv_id)
        started = clock.now()

    assert task.attempt == 2
    assert task.deadline == started + timedelta(seconds=180)


async def test_window_capped_by_investigation_deadline(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])
    app = make_app(
        FakeContextService(),
        agent,
        clock=clock,
        poll_interval_seconds=POLL,
        investigation_deadline_seconds=240,  # the task times out at 180 s: 60 s of window left
    )
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.RUNNING))
        await parked(clock)
        agent.ready = False
        clock.advance(TASK_DEADLINE)
        await wait_until(task_in(app, inv_id, T.TIMED_OUT))
        await parked(clock)

        clock.advance(59)
        await parked(clock)
        assert (await detail(client, inv_id))["status"] == "RUNNING"
        clock.advance(1)
        await wait_until(driver_done(app, inv_id))
        end = await detail(client, inv_id)

    assert end["status"] == "FAILED" and end["status_reason"] == "deadline_exceeded"
    assert len(agent.submits) == 1


async def test_redispatched_deadline_is_capped_by_investigation_deadline(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])
    app = make_app(
        FakeContextService(),
        agent,
        clock=clock,
        poll_interval_seconds=POLL,
        investigation_deadline_seconds=240,
    )
    async with running(app) as client:
        inv_id, _ = await time_out_first_attempt(app, client, clock)
        await wait_until(submits(agent, 2))
        await wait_until(task_in(app, inv_id, T.DISPATCHED, T.RUNNING))
        task = await the_task(app, inv_id)
        inv = await runtime(app).repo.investigation_record(inv_id)

    assert inv is not None and task.deadline == inv.deadline  # min(now + 180, inv.deadline)


# -- FR-122 -----------------------------------------------------------------------------------


async def test_attempt2_failure_terminal(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])
    app = make_app(FakeContextService(), agent, clock=clock, poll_interval_seconds=POLL)
    async with running(app) as client:
        inv_id, _ = await time_out_first_attempt(app, client, clock)
        await wait_until(submits(agent, 2))
        await wait_until(task_in(app, inv_id, T.DISPATCHED, T.RUNNING))
        await parked(clock)

        clock.advance(TASK_DEADLINE)  # attempt 2 runs out of time as well
        await wait_until(driver_done(app, inv_id))
        task = await the_task(app, inv_id)
        end = await detail(client, inv_id)

    assert task.status is T.TIMED_OUT and task.attempt == 2
    assert end["status"] == "FAILED" and end["status_reason"] == "deadline_exceeded"
    assert len(agent.submits) == 2  # no third POST


async def test_attempt2_agent_failure_is_not_retried_again(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING, S.FAILED], fail_code=ErrorCode.AGENT_RESTARTED)
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, driver_done(app, inv_id))
        task = await the_task(app, inv_id)
        end = await detail(client, inv_id)

    assert task.status is T.FAILED and task.attempt == 2
    assert end["status"] == "FAILED" and end["status_reason"] == "agent_restarted"
    assert len(agent.submits) == 2


async def test_internal_error_not_redispatched(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING, S.FAILED], fail_code=ErrorCode.INTERNAL_ERROR)
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await pump(clock, driver_done(app, inv_id))
        task = await the_task(app, inv_id)
        end = await detail(client, inv_id)

    assert task.status is T.FAILED and task.attempt == 1
    assert end["status"] == "FAILED" and end["status_reason"] == "internal_error"
    assert len(agent.submits) == 1
    assert agent.readyz_hits == 0  # not even asked: it is not retry-eligible


async def test_zero_reservation_not_redispatched(make_app, caplog) -> None:
    """Review Focus 4: attempt 1 already used the whole share, so there is nothing to retry with.
    The task stays final and its window counts as closed: no 180 s hold-up."""
    clock = FakeClock(utcnow())
    agent = FakeAgent(
        script=[S.WORKING, S.FAILED], fail_code=ErrorCode.AGENT_RESTARTED, usage=(10000, 5000)
    )
    app = make_app(FakeContextService(), agent, clock=clock)
    with caplog.at_level(logging.INFO, logger="nexus_orchestrator.workflow.poller"):
        async with running(app) as client:
            inv_id = await create(client)
            await pump(clock, driver_done(app, inv_id))
            task = await the_task(app, inv_id)
            end = await detail(client, inv_id)

    assert task.status is T.FAILED and task.attempt == 1
    assert end["status"] == "FAILED" and end["status_reason"] == "agent_restarted"
    assert len(agent.submits) == 1
    records = [r for r in caplog.records if "no token reservation left" in r.getMessage()]
    assert len(records) == 1  # once, not once per tick
    assert records[0].task_id == task.id and records[0].reservation == 0  # type: ignore[attr-defined]


# -- Review Focus 3: stale attempt ---------------------------------------------------------------


async def test_late_attempt1_result_ignored(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING, S.COMPLETED])
    agent.hold = True
    app = make_app(FakeContextService(), agent, clock=clock, poll_interval_seconds=POLL)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.DISPATCHED))
        await parked(clock)
        clock.advance(TASK_DEADLINE)  # attempt 1 times out; the agent is ready: attempt 2 starts
        await wait_until(submits(agent, 2))
        await wait_until(task_in(app, inv_id, T.DISPATCHED, T.RUNNING))
        await parked(clock)

        agent.reply_attempt = 1  # a late reply from attempt 1 now claims to be COMPLETED
        agent.hold = False
        polls_before = len(agent.polls)
        for _ in range(3):
            clock.advance(POLL)
            await parked(clock)
        assert len(agent.polls) > polls_before  # the poller really did look at it
        task = await the_task(app, inv_id)

    assert task.attempt == 2 and task.status in (T.DISPATCHED, T.RUNNING)


async def test_poll_one_ignores_a_result_for_another_attempt(make_app, caplog) -> None:
    """Defence in depth: ``AgentClient.get_task`` already drops these, so feed the poller one."""
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.RUNNING))
        await parked(clock)
        poller = runtime(app).poller
        task = await the_task(app, inv_id)
        stale = TaskResult(
            task_id=task.id,
            agent_id=task.agent_id,
            status=S.COMPLETED,
            attempt=task.attempt + 1,
            llm_mode=agent.mode,
            finding_ids=[],
        )

        async def late(*_a, **_k) -> TaskResult:
            return stale

        poller.agents.get_task = late  # type: ignore[method-assign]
        before = await events(client, inv_id, "task.status_changed")
        with caplog.at_level(logging.DEBUG, logger="nexus_orchestrator.workflow.poller"):
            await poller._poll_one(task, None)
        after = await events(client, inv_id, "task.status_changed")
        row = await the_task(app, inv_id)

    assert row.status is T.RUNNING and row.version == task.version
    assert len(after) == len(before)
    assert any("another attempt" in r.getMessage() for r in caplog.records)


# -- task_lost --------------------------------------------------------------------------------


async def test_task_lost_redispatch(make_app) -> None:
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING, S.COMPLETED])
    agent.hold = True
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.DISPATCHED))
        await parked(clock)
        agent.forget()  # the agent restarted and lost its state: the poll answers 404
        agent.hold = False
        await pump(clock, submits(agent, 2))
        await pump(clock, driver_done(app, inv_id))
        task = await the_task(app, inv_id)
        end = await detail(client, inv_id)
        changed = [e.payload for e in await events(client, inv_id, "task.status_changed")]

    assert task.attempt == 2 and task.status is T.COMPLETED
    assert end["status"] == "PARTIAL" and end["status_reason"] == "agent_not_enabled"
    lost = [p for p in changed if p["to_state"] == "FAILED"]
    assert [p["reason"] for p in lost] == ["task_lost"]


# -- concurrency: a concurrent writer wins ----------------------------------------------------


async def test_cancel_during_readiness_check_wins(make_app) -> None:
    """The readiness await runs outside any transaction. If the investigation is cancelled
    meanwhile, the write that follows sees it and the task is left alone."""
    clock = FakeClock(utcnow())
    agent = FakeAgent(script=[S.WORKING])
    app = make_app(FakeContextService(), agent, clock=clock, poll_interval_seconds=POLL)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.RUNNING))
        await parked(clock)
        rt = runtime(app)
        task = await the_task(app, inv_id)
        await rt.poller._time_out(task)
        timed_out = await the_task(app, inv_id)
        assert timed_out.status is T.TIMED_OUT
        inv = await rt.repo.investigation_record(inv_id)
        assert inv is not None
        manifest = await rt.poller.catalog.get(inv.scenario_id)

        async def ready_but_cancelled(_agent) -> bool:
            async with rt.db.transaction() as tx:  # the user cancels while we wait
                await rt.repo.transition_investigation(
                    tx, inv_id, InvestigationStatus.CANCELLED, now=clock.now()
                )
            return True

        rt.poller.agents.ready = ready_but_cancelled  # type: ignore[method-assign]
        await rt.poller._redispatch(timed_out, inv, manifest)
        row = await the_task(app, inv_id)

    assert row.status is T.TIMED_OUT and row.attempt == 1 and row.version == timed_out.version
    assert len(agent.submits) == 1
