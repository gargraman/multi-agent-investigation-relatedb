"""Resume sweep (S3b FR-127): a restarted orchestrator re-POSTs PENDING tasks with their stored
attempt and deadline, and gives every DISPATCHED/RUNNING task one poll **before** any deadline is
applied, so a result the agent finished during the outage is kept.

Each test seeds with one app (investigation created, task dispatched or left PENDING), edits the
rows or the agent as the outage would have, stops it, then starts a **second app over the same DB
file** whose clock starts ``outage`` seconds later. The resumed driver's first tick runs at that
time with no clock advance. ``FakeAgent`` is a double at the HTTP boundary, shared by both apps, so
it still "knows" the task after the restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from orch_testkit import (
    FakeAgent,
    FakeClock,
    FakeContextService,
    S,
    create,
    parked,
    pump,
    running,
    runtime,
    task_in,
    the_task,
    wait_until,
)

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus as I
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_orchestrator.ids import iso, utcnow
from nexus_orchestrator.workflow.dispatch import build_request


async def rewrite_task(app, task_id: str, **columns: object) -> None:
    sets = ", ".join(f"{name} = ?" for name in columns)
    async with runtime(app).db.transaction() as tx:
        await tx.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*columns.values(), task_id))


async def seed_dispatched(
    make_app, agent: FakeAgent, start: datetime, **columns: object
) -> tuple[str, str]:
    """First app: create an investigation and let its task reach DISPATCHED (the agent holds it
    at SUBMITTED); optionally rewrite columns of the row; stop. Returns ``(inv_id, task_id)``."""
    clock = FakeClock(start)
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.DISPATCHED))
        await parked(clock)
        task = await the_task(app, inv_id)
        if columns:
            await rewrite_task(app, task.id, **columns)
    return inv_id, task.id


async def seed_pending(
    make_app, agent: FakeAgent, start: datetime, **columns: object
) -> tuple[str, str]:
    """First app with the agent unreachable: one try (no backoff sleep), so the task stays
    PENDING; optionally rewrite columns of the row; stop. The agent is reachable again after."""
    agent.up = False
    clock = FakeClock(start)
    app = make_app(FakeContextService(), agent, clock=clock, dispatch_max_retries=0)
    async with running(app) as client:
        inv_id = await create(client)
        await parked(clock)
        task = await the_task(app, inv_id)
        assert task.status is T.PENDING
        if columns:
            await rewrite_task(app, task.id, **columns)
    agent.up = True
    return inv_id, task.id


def resumed_app(make_app, agent: FakeAgent, start: datetime, outage: float, **overrides):
    """Second app over the same DB, ``outage`` seconds after the first one's clock."""
    clock = FakeClock(start + timedelta(seconds=outage))
    return make_app(FakeContextService(), agent, clock=clock, **overrides), clock


async def wait_status(app, inv_id: str, *statuses: I) -> None:
    async def check() -> bool:
        record = await runtime(app).repo.investigation_record(inv_id)
        return record is not None and record.status in statuses

    await wait_until(check)


async def end_of(app, inv_id: str):
    investigation = await runtime(app).repo.get_investigation(inv_id)
    assert investigation is not None
    return investigation


# -- re-POST of PENDING --------------------------------------------------------------------------


async def test_resume_repost_pending_same_attempt_and_deadline(make_app) -> None:
    """A PENDING attempt-2 task is POSTed with attempt 2 and the stored deadline, not a new one."""
    start = utcnow()
    agent = FakeAgent()
    agent.hold = True
    stored = datetime(2031, 5, 6, 7, 8, 9, 123456, tzinfo=UTC)
    inv_id, task_id = await seed_pending(
        make_app, agent, start, attempt=2, deadline=iso(stored), token_reservation=12345
    )

    app, _clock = resumed_app(make_app, agent, start, 5)
    async with running(app):
        await wait_until(task_in(app, inv_id, T.DISPATCHED))
        task = await the_task(app, inv_id)
        inv = await runtime(app).repo.investigation_record(inv_id)
        manifest = await runtime(app).catalog.get(inv.scenario_id)

    [(code, body, _)] = agent.submits
    assert code == 202 and body.task_id == task_id
    assert body.attempt == 2 and body.token_budget == 12345
    assert body.deadline == stored
    # the request is a pure function of stored state, so a re-POST is byte-identical
    assert body.model_dump_json() == build_request(inv, task, manifest).model_dump_json()
    assert task.attempt == 2 and task.deadline == stored


async def test_resume_repost_is_byte_identical_to_the_original(make_app) -> None:
    """The agent dedups on (task_id, attempt) and 409s a different body: the resume re-POST of an
    already-delivered task must be a 200 duplicate."""
    start = utcnow()
    agent = FakeAgent()
    agent.hold = True
    inv_id, _ = await seed_dispatched(make_app, agent, start, status="PENDING", acked_at=None)

    app, _clock = resumed_app(make_app, agent, start, 5)
    async with running(app):
        await wait_until(task_in(app, inv_id, T.DISPATCHED))

    assert agent.statuses() == [202, 200]
    assert agent.submits[0][1].model_dump_json() == agent.submits[1][1].model_dump_json()


# -- poll first, then deadlines (FR-127) ----------------------------------------------------------


async def test_resume_polls_before_deadline_completed_kept(make_app) -> None:
    """The task deadline passed during the outage, but the agent finished: COMPLETED, not
    TIMED_OUT, and the agent is not told to cancel."""
    start = utcnow()
    agent = FakeAgent()
    agent.hold = True
    inv_id, task_id = await seed_dispatched(make_app, agent, start)
    agent.tasks[task_id]["status"] = S.COMPLETED  # finished while the orchestrator was down

    app, _clock = resumed_app(make_app, agent, start, 200)  # 180 s task deadline is past
    async with running(app):
        await wait_until(task_in(app, inv_id, T.COMPLETED, T.TIMED_OUT))
        task = await the_task(app, inv_id)

    assert task.status is T.COMPLETED and task.status_reason is None
    assert task.usage_tokens == 150  # the polled result was recorded
    assert agent.cancels == []


async def test_resume_polls_before_deadline_agent_timeout_is_timed_out(make_app) -> None:
    """FR-121b on resume: the agent reports FAILED(deadline_exceeded) during the outage; the
    poll maps it to TIMED_OUT (same code) without an orchestrator cancel."""
    start = utcnow()
    agent = FakeAgent(fail_code=ErrorCode.DEADLINE_EXCEEDED)
    agent.hold = True
    agent.ready = False  # no attempt 2: keep the TIMED_OUT row to look at
    inv_id, task_id = await seed_dispatched(make_app, agent, start)
    agent.tasks[task_id]["status"] = S.FAILED

    app, _clock = resumed_app(make_app, agent, start, 200)
    async with running(app):
        await wait_until(task_in(app, inv_id, T.TIMED_OUT, T.FAILED))
        task = await the_task(app, inv_id)

    assert task.status is T.TIMED_OUT and task.status_reason == "deadline_exceeded"
    assert task.attempt == 1
    assert agent.cancels == []  # it came from the poll, not from the orchestrator's timeout


async def test_resume_no_answer_then_timed_out(make_app) -> None:
    """A poll with no terminal answer leaves the task for the deadline step of the same tick."""
    start = utcnow()
    agent = FakeAgent()
    agent.hold = True
    agent.ready = False
    inv_id, task_id = await seed_dispatched(make_app, agent, start)
    polls_before = len(agent.polls)

    app, _clock = resumed_app(make_app, agent, start, 200)
    async with running(app):
        await wait_until(task_in(app, inv_id, T.TIMED_OUT))
        task = await the_task(app, inv_id)

    assert task.status is T.TIMED_OUT and task.status_reason == "deadline_exceeded"
    assert len(agent.polls) == polls_before + 1  # asked once before giving up
    assert agent.cancels == [task_id]


# -- investigation deadline passed during the outage (Review Focus 5) ------------------------------


async def test_resume_after_investigation_deadline_keeps_result_then_partial(make_app) -> None:
    """Polled first: the finished specialist counts, so the fence ends it PARTIAL."""
    start = utcnow()
    agent = FakeAgent()
    agent.hold = True
    inv_id, task_id = await seed_dispatched(make_app, agent, start)
    agent.tasks[task_id]["status"] = S.COMPLETED

    app, _clock = resumed_app(make_app, agent, start, 700)  # past the 600 s investigation deadline
    async with running(app):
        await wait_status(app, inv_id, I.PARTIAL, I.FAILED)
        task = await the_task(app, inv_id)
        end = await end_of(app, inv_id)

    assert task.status is T.COMPLETED
    assert end.status is I.PARTIAL and end.status_reason == "investigation_deadline"
    assert agent.cancels == [] and len(agent.submits) == 1


async def test_resume_after_investigation_deadline_no_answer_is_failed(make_app) -> None:
    """No terminal answer: FAILED(investigation_deadline), the open task is cancelled."""
    start = utcnow()
    agent = FakeAgent()
    agent.hold = True
    inv_id, task_id = await seed_dispatched(make_app, agent, start)
    polls_before = len(agent.polls)

    app, _clock = resumed_app(make_app, agent, start, 700)
    async with running(app):
        await wait_status(app, inv_id, I.PARTIAL, I.FAILED)
        task = await the_task(app, inv_id)
        end = await end_of(app, inv_id)

    assert len(agent.polls) == polls_before + 1  # polled before it gave up
    assert task.status is T.CANCELLED
    assert end.status is I.FAILED and end.status_reason == "investigation_deadline"
    assert agent.cancels == [task_id] and len(agent.submits) == 1


async def test_resume_after_investigation_deadline_does_not_repost_pending(make_app) -> None:
    start = utcnow()
    agent = FakeAgent()
    agent.hold = True
    inv_id, _ = await seed_pending(make_app, agent, start)

    app, _clock = resumed_app(make_app, agent, start, 700)
    async with running(app):
        await wait_status(app, inv_id, I.PARTIAL, I.FAILED)
        end = await end_of(app, inv_id)

    assert end.status is I.FAILED and end.status_reason == "investigation_deadline"
    assert agent.submits == []  # no re-dispatch POST


async def test_ensure_resumed_is_a_noop_for_a_running_driver(make_app) -> None:
    """``resumed`` only shapes a new driver's first tick; it never restarts an existing one."""
    clock = FakeClock(utcnow())
    agent = FakeAgent()
    agent.hold = True
    app = make_app(FakeContextService(), agent, clock=clock)
    async with running(app) as client:
        inv_id = await create(client)
        await wait_until(task_in(app, inv_id, T.DISPATCHED))
        await parked(clock)
        poller = runtime(app).poller
        before = poller._drivers[inv_id]
        poller.ensure(inv_id, resumed=True)
        assert poller._drivers[inv_id] is before


async def test_resume_poll_failure_keeps_poll_first_for_the_retry(make_app, monkeypatch) -> None:
    """The poll on the first resumed tick raises: the retry must still be poll-first, so the
    result the agent finished during the outage is kept instead of timed out."""
    start = utcnow()
    agent = FakeAgent()
    agent.hold = True
    agent.ready = False  # a timed-out task stays TIMED_OUT (no attempt 2) so it can be seen
    inv_id, task_id = await seed_dispatched(make_app, agent, start)
    agent.tasks[task_id]["status"] = S.COMPLETED

    app, clock = resumed_app(make_app, agent, start, 200)  # task deadline is past
    poller = runtime(app).poller
    real_poll = poller._poll_one
    calls: list[str] = []

    async def flaky_poll(task, trace_context_json) -> None:
        calls.append(task.id)
        if len(calls) == 1:
            raise RuntimeError("poll failed (test double)")
        await real_poll(task, trace_context_json)

    monkeypatch.setattr(poller, "_poll_one", flaky_poll)
    async with running(app):

        async def failed_once() -> bool:
            return len(calls) == 1

        await wait_until(failed_once)
        await parked(clock)
        assert (await the_task(app, inv_id)).status is T.DISPATCHED  # that tick timed nothing out
        await pump(clock, task_in(app, inv_id, T.COMPLETED, T.TIMED_OUT))
        task = await the_task(app, inv_id)

    assert task.status is T.COMPLETED
    assert agent.cancels == []


@pytest.mark.parametrize("outage", [0, 5])
async def test_resume_with_nothing_expired_polls_and_carries_on(make_app, outage: float) -> None:
    """The ordinary restart: nothing is past its deadline, the task just keeps being polled."""
    start = utcnow()
    agent = FakeAgent()
    agent.hold = True
    inv_id, task_id = await seed_dispatched(make_app, agent, start)
    agent.tasks[task_id]["status"] = S.WORKING

    app, _clock = resumed_app(make_app, agent, start, outage)
    async with running(app):
        await wait_until(task_in(app, inv_id, T.RUNNING))
        task = await the_task(app, inv_id)

    assert task.attempt == 1 and agent.cancels == []
