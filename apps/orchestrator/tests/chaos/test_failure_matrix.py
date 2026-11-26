"""In-process chaos suite for the S3 failure matrix (spec §12, AC12-b, AC13-d, NFR-105).

One test per §12 row that has an in-process owner. The real orchestrator runs against two
``FakeAgent`` doubles (SIGINT, GEOINT), a ``FakeContextService`` and a ``FakeClock``; every test
drives virtual time through ``Session.drive`` and asserts the final investigation status/reason
plus the task attempt/status rows. The doubles sit at the HTTP boundary, so this proves the
orchestrator's policy, not the real agents (that is ``make chaos``, WP-E).

Defaults: task deadline 180 s, investigation deadline 600 s, one tick per 5 virtual seconds. SIGINT
always finishes on its second poll; the knobs go on GEOINT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from orch_testkit import S, runtime, wait_until

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus as I
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_contracts.common import AgentId

SIG, GEO = AgentId.SIGINT, AgentId.GEOINT
SHARE_RESERVATION = 15000  # floor(0.25 x 60000): both specialists in baseline-corroboration
TASK_DEADLINE = 180.0
INVESTIGATION_DEADLINE = 600.0


async def test_ac12b_never_answers_twice(chaos_app) -> None:
    """AC12-b: attempt 1 times out, attempt 2 gets a fresh deadline and the share's reservation,
    and its own timeout is final: PARTIAL(deadline_exceeded) with SIGINT's findings kept."""
    chaos_app.geo.hang()
    async with chaos_app.session() as s:
        inv_id = await s.create()
        started = chaos_app.clock.now()
        inv = await s.drive_until_terminal(inv_id)
        ended = chaos_app.clock.now()
        tasks = await s.tasks(inv_id)
        changes = await s.changes(inv_id, GEO)

    assert (inv.status, inv.status_reason) == (I.PARTIAL, ErrorCode.DEADLINE_EXCEEDED)
    # the second timeout is final at once: no wait window is held open after attempt 2 (FR-122)
    assert ended - started <= timedelta(seconds=2 * TASK_DEADLINE + 3 * chaos_app.poll)
    geo, sig = tasks[GEO], tasks[SIG]
    assert (geo.attempt, geo.status, geo.status_reason) == (2, T.TIMED_OUT, "deadline_exceeded")
    assert sig.status is T.COMPLETED and sig.attempt == 1
    assert [f for t in inv.tasks if t.agent_id == SIG for f in t.finding_ids]  # SIGINT's kept
    assert [f for t in inv.tasks if t.agent_id == GEO for f in t.finding_ids] == []

    assert [(a, f, t) for a, f, t, _ in changes if f and t in {"TIMED_OUT", "PENDING"}] == [
        (1, "RUNNING", "TIMED_OUT"),
        (2, "TIMED_OUT", "PENDING"),
        (2, "RUNNING", "TIMED_OUT"),
    ]
    # attempt 2 was submitted with a fresh deadline (a full task window after the retry), and the
    # reservation is the share minus attempt 1's usage: a hung agent reports none, so it is whole
    assert chaos_app.geo.statuses() == [202, 202]
    (_, first, _), (_, second, _) = chaos_app.geo.submits
    assert (first.attempt, second.attempt) == (1, 2)
    assert first.deadline <= started + timedelta(seconds=TASK_DEADLINE + 1)
    assert second.deadline > first.deadline + timedelta(seconds=TASK_DEADLINE - 10)
    assert second.token_budget == SHARE_RESERVATION == geo.token_reservation
    assert len(chaos_app.geo.cancels) == 2  # best-effort cancel after each timeout, never a 3rd try


async def test_agent_down_before_ack(chaos_app) -> None:
    """Agent stopped before ack: PENDING until its deadline, TIMED_OUT, the retry window waits for
    /readyz and closes with the agent still down, so the timeout is final."""
    chaos_app.geo.down()
    async with chaos_app.session() as s:
        inv_id = await s.create()
        inv = await s.drive_until_terminal(inv_id)
        tasks = await s.tasks(inv_id)
        changes = await s.changes(inv_id, GEO)

    assert (inv.status, inv.status_reason) == (I.PARTIAL, ErrorCode.DEADLINE_EXCEEDED)
    geo = tasks[GEO]
    assert (geo.attempt, geo.status, geo.status_reason) == (1, T.TIMED_OUT, "deadline_exceeded")
    assert tasks[SIG].status is T.COMPLETED
    assert [(f, t) for _, f, t, _ in changes] == [(None, "PENDING"), ("PENDING", "TIMED_OUT")]
    assert chaos_app.geo.submits == []  # nothing ever reached the agent, no re-dispatch
    assert (
        chaos_app.geo.cancels == []
    )  # it never acknowledged a task, so there is nothing to cancel


async def test_agent_restarted_mid_task(chaos_app) -> None:
    """Agent restarted mid-task: the poll sees FAILED(agent_restarted), attempt 2 runs to
    COMPLETED with the reservation reduced by attempt 1's reported usage."""
    async with chaos_app.session() as s:
        inv_id = await s.create()
        await s.settle()
        chaos_app.geo.restart()
        inv = await s.drive_until_terminal(inv_id)
        tasks = await s.tasks(inv_id)
        changes = await s.changes(inv_id, GEO)

    assert (inv.status, inv.status_reason) == (I.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)
    geo = tasks[GEO]
    assert (geo.attempt, geo.status) == (2, T.COMPLETED)
    assert tasks[SIG].status is T.COMPLETED
    assert [
        (a, t, r) for a, f, t, r in changes if f and t in {"FAILED", "PENDING", "COMPLETED"}
    ] == [
        (1, "FAILED", "agent_restarted"),
        (2, "PENDING", None),
        (2, "COMPLETED", None),
    ]
    (_, _, _), (_, second, _) = chaos_app.geo.submits
    assert second.attempt == 2
    assert second.token_budget == SHARE_RESERVATION - sum(chaos_app.geo.usage)  # 15000 - 150


async def test_task_lost(chaos_app) -> None:
    """Agent lost the task (state wiped): the poll 404s, FAILED(task_lost), attempt 2 completes."""
    async with chaos_app.session() as s:
        inv_id = await s.create()
        await s.settle()
        chaos_app.geo.forget()
        inv = await s.drive_until_terminal(inv_id)
        tasks = await s.tasks(inv_id)
        changes = await s.changes(inv_id, GEO)

    assert (inv.status, inv.status_reason) == (I.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)
    assert (tasks[GEO].attempt, tasks[GEO].status) == (2, T.COMPLETED)
    assert [
        (a, t, r) for a, f, t, r in changes if f and t in {"FAILED", "PENDING", "COMPLETED"}
    ] == [
        (1, "FAILED", "task_lost"),
        (2, "PENDING", None),
        (2, "COMPLETED", None),
    ]
    assert chaos_app.geo.statuses() == [202, 202]


async def test_ac13d_investigation_deadline(chaos_factory) -> None:
    """AC13-d: SIGINT completed, GEOINT hangs on both attempts past the investigation deadline.
    Attempt 1 gets 400 s, so attempt 2 (capped at the investigation deadline) is still open at
    600 s: the fence cancels it and the investigation ends PARTIAL(investigation_deadline)."""
    chaos = chaos_factory(task_deadline_seconds=400)
    chaos.geo.hang()
    async with chaos.session() as s:
        inv_id = await s.create()
        created = chaos.clock.now()
        inv = await s.drive_until_terminal(inv_id)
        ended = chaos.clock.now()
        tasks = await s.tasks(inv_id)
        record = await runtime(s.app).repo.investigation_record(inv_id)

    assert (inv.status, inv.status_reason) == (I.PARTIAL, ErrorCode.INVESTIGATION_DEADLINE)
    geo = tasks[GEO]
    assert (geo.attempt, geo.status) == (2, T.CANCELLED)  # the retry was running when time ran out
    assert tasks[SIG].status is T.COMPLETED
    assert len(chaos.geo.cancels) == 2  # timeout of attempt 1, then the fence
    assert ended - created <= timedelta(seconds=INVESTIGATION_DEADLINE + 5.0)  # NFR-100
    assert record is not None and record.context_closed is True
    assert chaos.ctx.closes == [(inv_id, "PARTIAL")]


async def test_cancel_cs_down_then_reconciled(chaos_app) -> None:
    """CS down during cancel: CANCELLED at once with context_closed false; once the Context
    Service is back the reconciler's next pass (30 s) closes it."""
    async with chaos_app.session() as s:
        inv_id = await s.create()
        await s.settle()
        chaos_app.ctx.up = False

        resp = await s.client.post(f"/api/v1/investigations/{inv_id}/cancel")
        before = await runtime(s.app).repo.investigation_record(inv_id)

        chaos_app.ctx.up = True
        await s.drive(30)
        await _closed(s, inv_id)
        after = await runtime(s.app).repo.investigation_record(inv_id)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": inv_id, "status": "CANCELLED", "context_closed": False}
    assert before is not None and (before.status, before.context_closed) == (I.CANCELLED, False)
    assert after is not None and (after.status, after.context_closed) == (I.CANCELLED, True)
    assert chaos_app.ctx.closes == [(inv_id, "CANCELLED")]  # the failed attempt never landed


async def _closed(s, inv_id: str) -> None:
    """The reconciler's write is DB-backed: wait for it, as drive() only sees the sleepers."""

    async def check() -> bool:
        record = await runtime(s.app).repo.investigation_record(inv_id)
        return record is not None and record.context_closed

    await wait_until(check)


@dataclass
class Outcome:
    status: I
    reason: ErrorCode | None
    attempts: list[int]
    findings: dict[AgentId, list[str]]
    posts: int  # task submissions that reached either agent


async def _run_to_end(chaos, *, outage: float | None) -> Outcome:
    """One run of the scenario: GEOINT answers 30 s after its ack. With ``outage``, the
    orchestrator is stopped right after dispatch and a new one is started over the same DB that
    many virtual seconds later (the agents keep running and keep their state)."""
    chaos.geo.answer_after(30, chaos.clock)
    async with chaos.session() as s:
        inv_id = await s.create()
        await s.settle()  # dispatched and polled once: both tasks are RUNNING
        if outage is None:
            inv = await s.drive_until_terminal(inv_id)
    if outage is not None:
        chaos.clock.advance(outage)  # GEOINT's answer is ready, nobody asks until the restart
        async with chaos.session() as s:
            inv = await s.drive_until_terminal(inv_id)
    return Outcome(
        status=inv.status,
        reason=inv.status_reason,
        attempts=[t.attempt for t in inv.tasks],
        findings={t.agent_id: sorted(t.finding_ids) for t in inv.tasks},
        posts=len(chaos.sig.submits) + len(chaos.geo.submits),
    )


async def test_orchestrator_restart_mid_run(chaos_factory) -> None:
    """Orchestrator killed after dispatch: the resume sweep finishes the run with the same terminal
    status as an uninterrupted run (computed here, with identically configured fakes), one
    finding-id set per task and no duplicate dispatch."""
    baseline, resumed = chaos_factory(), chaos_factory()
    expected = await _run_to_end(baseline, outage=None)
    got = await _run_to_end(resumed, outage=40)

    assert (expected.status, expected.reason) == (I.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)
    assert (got.status, got.reason) == (expected.status, expected.reason)
    assert got.attempts == expected.attempts == [1, 1]
    assert got.posts == expected.posts == 2  # one POST per agent: the resume re-POSTs nothing
    for chaos, outcome in ((baseline, expected), (resumed, got)):
        assert set(outcome.findings) == {SIG, GEO}
        every = [f for ids in outcome.findings.values() for f in ids]
        assert len(every) == 4 and len(set(every)) == 4  # one set per task, no duplicates
        for agent, fake in ((SIG, chaos.sig), (GEO, chaos.geo)):
            suffix = fake.submits[0][1].task_id.removeprefix("tsk_")
            assert outcome.findings[agent] == [f"fnd_{suffix}a", f"fnd_{suffix}b"]


async def test_internal_error_not_redispatched(chaos_app) -> None:
    """Internal agent bug: FAILED(internal_error) is final (one POST, attempt 1); SIGINT's
    completion makes the investigation PARTIAL(internal_error)."""
    chaos_app.geo.script = [S.WORKING, S.FAILED]
    chaos_app.geo.fail_code = ErrorCode.INTERNAL_ERROR
    async with chaos_app.session() as s:
        inv_id = await s.create()
        started = chaos_app.clock.now()
        inv = await s.drive_until_terminal(inv_id)
        ended = chaos_app.clock.now()
        tasks = await s.tasks(inv_id)
        await s.drive(TASK_DEADLINE)  # a retry window would have been open for 180 s: stay shut
        submits_after = list(chaos_app.geo.submits)

    assert (inv.status, inv.status_reason) == (I.PARTIAL, ErrorCode.INTERNAL_ERROR)
    assert ended - started <= timedelta(seconds=3 * chaos_app.poll)  # no retry window held open
    geo = tasks[GEO]
    assert (geo.attempt, geo.status, geo.status_reason) == (1, T.FAILED, "internal_error")
    assert tasks[SIG].status is T.COMPLETED
    assert chaos_app.geo.statuses() == [202] == [c for c, _, _ in submits_after]
