"""S2 workflow end to end against the in-process ``FakeAgent`` double (S2 FR-002..FR-010).

Create → tasks inserted → dispatch → poll → terminal rule, plus restart, leader, E4 lock and the
findings proxy. The agent is a test double (``orch_testkit.FakeAgent``); the real SIGINT agent
runs in Compose.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from opentelemetry.trace import SpanKind
from orch_testkit import (
    SCENARIO,
    SCENARIOS_DIR,
    TOKEN,
    FakeAgent,
    FakeContextService,
    S,
    running,
    runtime,
    wait_until,
)

from nexus_contracts.api.context import InvestigationContext
from nexus_contracts.api.investigations import Investigation
from nexus_contracts.api.states import OrchestratorTaskState
from nexus_contracts.common import Classification, LlmMode
from nexus_contracts.events.timeline import TimelineEvent
from nexus_contracts.scenario.loader import load_manifest
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.repository import Applied

TERMINAL = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}


async def create(client, **extra) -> Investigation:
    resp = await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO, **extra})
    assert resp.status_code == 201, resp.text
    return Investigation.model_validate(resp.json())


async def detail(client, inv_id: str) -> Investigation:
    return Investigation.model_validate(
        (await client.get(f"/api/v1/investigations/{inv_id}")).json()
    )


async def timeline(client, inv_id: str) -> list[TimelineEvent]:
    resp = await client.get(f"/api/v1/investigations/{inv_id}/timeline", params={"limit": 100})
    return [TimelineEvent.model_validate(e) for e in resp.json()["items"]]


async def until_status(client, inv_id: str, statuses: set[str]) -> Investigation:
    async def ok() -> bool:
        return (await detail(client, inv_id)).status in statuses

    await wait_until(ok)
    return await detail(client, inv_id)


async def until_task(client, inv_id: str, state: str) -> None:
    async def ok() -> bool:
        return (await detail(client, inv_id)).tasks[0].status == state

    await wait_until(ok)


def task_states(events: list[TimelineEvent]) -> list[tuple[str | None, str]]:
    return [
        (e.payload["from_state"], e.payload["to_state"])
        for e in events
        if e.type == "task.status_changed"
    ]


# -- happy path -------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [LlmMode.LIVE, LlmMode.REPLAY])
async def test_happy_path_ends_partial_agent_not_enabled(make_app, mode: LlmMode) -> None:
    agent = FakeAgent(mode=mode, script=[S.WORKING, S.WORKING, S.WORKING, S.COMPLETED])
    app = make_app(FakeContextService(), agent)
    async with running(app) as client:
        inv = await create(client)
        done = await until_status(client, inv.id, TERMINAL)
        events = await timeline(client, inv.id)

    assert done.status == "PARTIAL" and done.status_reason == "agent_not_enabled"
    [task] = done.tasks
    assert task.status == "COMPLETED"
    assert task.llm_mode == mode
    assert task.finding_ids == [f"fnd_{task.id[4:]}a", f"fnd_{task.id[4:]}b"]
    assert task.usage is not None and task.usage.input_tokens == 120
    assert task.error_code is None
    assert done.tokens_used == 150

    # One event per transition, never one per poll: 4 polls, 3 of them WORKING.
    assert len(agent.polls) >= 4
    assert task_states(events) == [
        (None, "PENDING"),
        ("PENDING", "DISPATCHED"),
        ("DISPATCHED", "RUNNING"),
        ("RUNNING", "COMPLETED"),
    ]
    inv_events = [e.payload for e in events if e.type == "investigation.status_changed"]
    assert [(p["from_status"], p["to_status"]) for p in inv_events] == [
        (None, "CREATED"),
        ("CREATED", "RUNNING"),
        ("RUNNING", "PARTIAL"),
    ]
    assert inv_events[1]["skipped_agents"] == ["geoint-agent", "analysis-agent"]
    assert inv_events[2]["reason"] == "agent_not_enabled"
    replayed = [e.payload for e in events if e.type == "llm.replayed"]
    if mode is LlmMode.REPLAY:
        assert replayed == [
            {"task_id": task.id, "agent_id": "sigint-agent", "model_id": "fake-model-1"}
        ]
    else:
        assert replayed == []
    assert not [e for e in events if e.type == "transition.dropped"]


async def test_task_request_fields_trace_context_and_token(make_app, exporter) -> None:
    agent = FakeAgent()
    agent.hold = True
    app = make_app(FakeContextService(), agent)
    async with running(app) as client:
        before = utcnow()
        inv = await create(client)
        await until_task(client, inv.id, "DISPATCHED")
        await wait_until(lambda: _gt(agent.polls, 2))
        task = (await detail(client, inv.id)).tasks[0]

    [(code, body, headers)] = agent.submits
    assert code == 202
    manifest = load_manifest(SCENARIOS_DIR / SCENARIO)
    sig = next(t for t in manifest.template.tasks if t.agent == "sigint-agent")
    assert body.task_id == task.id and body.investigation_id == inv.id
    assert body.task_type == sig.task_type == "analyze_synthetic_communications"
    assert body.input_refs == list(sig.input_refs)
    assert body.scenario_id == SCENARIO
    assert body.objective == manifest.objective
    assert body.attempt == 1
    assert body.token_budget == 15000  # floor(0.25 × 60000)
    assert body.contract_version == "1.0"
    by_id = {e.id: e for e in manifest.evidence}
    ceiling = max((by_id[r].classification for r in sig.input_refs), key=lambda c: c.rank)
    assert body.classification_ceiling == ceiling
    assert body.classification_ceiling in set(Classification)
    assert timedelta(seconds=179) < body.deadline - before < timedelta(seconds=181)
    assert body.deadline == task.deadline

    assert headers["authorization"] == f"Bearer {TOKEN}"
    spans = exporter.get_finished_spans()
    create_span = next(s for s in spans if s.name == "workflow.create")
    trace_id = format(create_span.context.trace_id, "032x")
    assert body.trace_context.traceparent.split("-")[1] == trace_id
    assert headers["traceparent"].split("-")[1] == trace_id  # instrumented client
    submit = next(s for s in spans if s.name == "a2a.submit")
    assert submit.context.trace_id == create_span.context.trace_id
    assert submit.attributes["nexus.task.id"] == task.id
    assert submit.attributes["nexus.dispatch.outcome"] == "acked"
    # No span per poll: polls are uninstrumented (ADR-008 F9). Polls happened, GET spans did not.
    methods = [
        s.attributes.get("http.request.method", s.attributes.get("http.method"))
        for s in spans
        if s.kind is SpanKind.CLIENT
    ]
    assert "POST" in methods and "GET" not in methods


# -- failure outcomes -------------------------------------------------------------------------


async def test_agent_failed_ends_failed_with_the_tasks_reason(make_app) -> None:
    agent = FakeAgent(script=[S.WORKING, S.FAILED])
    app = make_app(FakeContextService(), agent)
    async with running(app) as client:
        inv = await create(client)
        done = await until_status(client, inv.id, TERMINAL)
    assert done.status == "FAILED" and done.status_reason == "llm_unavailable"
    [task] = done.tasks
    assert task.status == "FAILED"
    assert task.error_code == "llm_unavailable" and task.error_category == "llm"
    assert task.finding_ids == []
    assert done.tokens_used == 150  # a failed attempt's usage is real spend


async def test_rejected_on_submit_fails_task_with_task_rejected(make_app) -> None:
    agent = FakeAgent(submit_status=S.REJECTED)
    app = make_app(FakeContextService(), agent)
    async with running(app) as client:
        inv = await create(client)
        done = await until_status(client, inv.id, TERMINAL)
        events = await timeline(client, inv.id)
    assert done.status == "FAILED" and done.status_reason == "task_rejected"
    assert done.tasks[0].status == "FAILED"
    assert done.tasks[0].error_code == "task_rejected"
    assert task_states(events)[-1] == ("PENDING", "FAILED")
    assert agent.polls == []


async def test_agent_losing_the_task_fails_it_with_task_lost(make_app) -> None:
    agent = FakeAgent()
    agent.hold = True
    agent.ready = False  # S3b: with a ready agent the task would be re-dispatched (test_redispatch)
    app = make_app(FakeContextService(), agent)
    async with running(app) as client:
        inv = await create(client)
        await until_task(client, inv.id, "DISPATCHED")
        agent.forget()  # the agent restarted and lost its in-memory state
        await until_task(client, inv.id, "FAILED")
        done = await detail(client, inv.id)
    assert done.tasks[0].status == "FAILED" and done.tasks[0].error_code == "task_lost"
    assert done.status == "RUNNING"  # held open by the retry wait window (FR-121a)


async def test_no_enabled_agent_fails_inside_create(make_app) -> None:
    agent = FakeAgent()
    app = make_app(FakeContextService(), agent, nexus_enabled_agents=())
    async with running(app) as client:
        inv = await create(client)
        events = await timeline(client, inv.id)
    assert inv.status == "FAILED" and inv.status_reason == "agent_not_enabled"
    assert inv.tasks == []
    payloads = [e.payload for e in events]
    assert [(p["from_status"], p["to_status"]) for p in payloads] == [
        (None, "CREATED"),
        ("CREATED", "RUNNING"),
        ("RUNNING", "FAILED"),
    ]
    assert payloads[1]["skipped_agents"] == ["sigint-agent", "geoint-agent", "analysis-agent"]
    assert agent.submits == []


async def test_unreachable_agent_leaves_task_pending_with_a_timeline_note(
    make_app, exporter
) -> None:
    agent = FakeAgent()
    agent.up = False
    app = make_app(FakeContextService(), agent)
    async with running(app) as client:
        inv = await create(client)

        async def noted() -> bool:
            return any(e.type == "transition.dropped" for e in await timeline(client, inv.id))

        await wait_until(noted)
        now = await detail(client, inv.id)
        events = await timeline(client, inv.id)
    assert now.status == "RUNNING" and now.tasks[0].status == "PENDING"
    [note] = [e.payload for e in events if e.type == "transition.dropped"]
    assert note["reason"] == "agent_unreachable" and note["tries"] == 4
    assert note["current_state"] == "PENDING" and note["attempted_state"] == "DISPATCHED"
    submits = [s for s in exporter.get_finished_spans() if s.name == "a2a.submit"]
    assert [s.attributes["nexus.dispatch.try"] for s in submits] == [1, 2, 3, 4]
    assert {s.attributes["nexus.dispatch.outcome"] for s in submits} == {"unavailable"}


# -- restart / leader -------------------------------------------------------------------------


async def test_restart_redispatch_is_idempotent_via_200_duplicate(make_app) -> None:
    """The agent got every POST but the replies were lost. After a restart the resume sweep
    re-POSTs the same body (same attempt); the agent answers 200 with its current status, and
    the run completes with no new attempt and no conflict."""
    fake = FakeContextService()
    agent = FakeAgent()
    agent.lose_response = True
    app = make_app(fake, agent)
    async with running(app) as client:
        inv = await create(client)

        async def noted() -> bool:
            return any(e.type == "transition.dropped" for e in await timeline(client, inv.id))

        await wait_until(noted)
        assert (await detail(client, inv.id)).tasks[0].status == "PENDING"
    assert agent.statuses() == [202, 200, 200, 200]

    agent.lose_response = False
    app2 = make_app(fake, agent)
    async with running(app2) as client:
        done = await until_status(client, inv.id, TERMINAL)
        events = await timeline(client, inv.id)
    assert agent.statuses() == [202, 200, 200, 200, 200]
    bodies = {b.model_dump_json() for _, b, _ in agent.submits}
    assert len(bodies) == 1  # byte-identical request, so no 409 content_conflict
    assert done.status == "PARTIAL" and done.tasks[0].attempt == 1
    assert task_states(events)[1:] == [
        ("PENDING", "DISPATCHED"),
        ("DISPATCHED", "RUNNING"),
        ("RUNNING", "COMPLETED"),
    ]


async def test_polling_resumes_after_restart(make_app) -> None:
    fake = FakeContextService()
    agent = FakeAgent()
    agent.hold = True
    app = make_app(fake, agent)
    async with running(app) as client:
        inv = await create(client)
        await until_task(client, inv.id, "DISPATCHED")
    agent.hold = False
    app2 = make_app(fake, agent)
    async with running(app2) as client:
        done = await until_status(client, inv.id, TERMINAL)
    assert done.status == "PARTIAL"
    assert agent.statuses() == [202]  # DISPATCHED tasks are polled, not re-POSTed


async def test_poller_and_dispatch_run_only_on_the_leader(make_app) -> None:
    agent = FakeAgent()
    agent.hold = True
    app = make_app(FakeContextService(), agent)
    async with running(app) as client:
        rt = runtime(app)
        rt.leader._held = False  # lost the lock (heartbeat failure)
        inv = await create(client)
        await asyncio.sleep(0.2)
        assert agent.submits == [] and agent.polls == [] and rt.poller.polls == 0
        assert (await detail(client, inv.id)).tasks[0].status == "PENDING"

        rt.leader._held = True
        await until_task(client, inv.id, "DISPATCHED")
        await wait_until(lambda: _gt(agent.polls, 0))

        rt.leader._held = False
        await asyncio.sleep(0.1)  # let an in-flight tick finish
        frozen = len(agent.polls)
        await asyncio.sleep(0.2)
        assert len(agent.polls) == frozen
        assert rt.poller.active() == {inv.id}  # the driver waits, it does not exit

        rt.leader._held = True
        agent.hold = False
        done = await until_status(client, inv.id, TERMINAL)
    assert done.status == "PARTIAL"


async def _gt(items: list, n: int) -> bool:
    return len(items) > n


# -- E4 objective lock ------------------------------------------------------------------------


async def test_replay_mode_locks_the_objective(make_app) -> None:
    agent = FakeAgent(mode=LlmMode.REPLAY)
    agent.hold = True
    app = make_app(FakeContextService(), agent)
    manifest = load_manifest(SCENARIOS_DIR / SCENARIO)
    async with running(app) as client:
        resp = await client.post(
            "/api/v1/investigations", json={"scenario_id": SCENARIO, "objective": "Other aim"}
        )
        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.json()["code"] == "replay_objective_locked"
        await create(client)  # template objective, implicitly
        await create(client, objective=manifest.objective)  # ... and explicitly

        # The 30 s card refresh (fast here) picks up a switch to live.
        agent.mode = LlmMode.LIVE
        rt = runtime(app)

        async def unlocked() -> bool:
            return not rt.registry.replay_locked

        await wait_until(unlocked)
        await create(client, objective="Other aim")


async def test_live_mode_and_unreachable_card_do_not_lock(make_app) -> None:
    agent = FakeAgent()
    agent.hold = True
    app = make_app(FakeContextService(), agent)
    async with running(app) as client:
        await create(client, objective="Other aim")
        agent.up = False  # card unreadable: fail open, keep the last-known mode (live)
        await asyncio.sleep(0.15)
        await create(client, objective="Another aim")


async def test_health_reports_agent_down_as_degraded(make_app) -> None:
    agent = FakeAgent()
    agent.up = False
    app = make_app(FakeContextService(), agent)
    async with running(app) as client:
        body = (await client.get("/api/v1/system/health")).json()
    assert body["status"] == "DEGRADED"
    [comp] = [c for c in body["components"] if c["name"] == "sigint-agent"]
    assert comp["status"] == "DOWN" and comp["detail"]


# -- findings proxy ---------------------------------------------------------------------------


async def test_findings_proxy(make_app) -> None:
    fake = FakeContextService()
    agent = FakeAgent()
    agent.hold = True
    app = make_app(fake, agent)
    async with running(app) as client:
        inv = await create(client)
        resp = await client.get(f"/api/v1/investigations/{inv.id}/findings")
        assert resp.status_code == 200
        ctx = InvestigationContext.model_validate(resp.json())
        assert ctx.investigation_id == inv.id and ctx.findings == []
        assert fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"

        missing = await client.get("/api/v1/investigations/inv_NOPE/findings")
        assert missing.status_code == 404

        fake.context_status = 500
        bad = await client.get(f"/api/v1/investigations/{inv.id}/findings")
        assert bad.status_code == 503 and bad.json()["code"] == "context_unavailable"

        fake.context_status = 200
        fake.up = False
        down = await client.get(f"/api/v1/investigations/{inv.id}/findings")
        assert down.status_code == 503 and down.json()["code"] == "context_unavailable"


async def test_findings_proxy_404_when_not_registered_with_context_service(make_app) -> None:
    fake = FakeContextService(up=False)
    app = make_app(fake, context_retry_interval_seconds=60)
    async with running(app) as client:
        inv = await create(client)
        assert inv.status == "CREATED"
        fake.up = True
        resp = await client.get(f"/api/v1/investigations/{inv.id}/findings")
    assert resp.status_code == 404


# -- repository guards ------------------------------------------------------------------------


async def test_stale_writes_are_dropped_or_noop_never_applied(make_app) -> None:
    app = make_app(FakeContextService(), FakeAgent())
    async with running(app) as client:
        inv = await create(client)
        await until_status(client, inv.id, TERMINAL)
        rt = runtime(app)
        [current] = await rt.repo.task_records(inv.id)
        assert current.status is OrchestratorTaskState.COMPLETED
        stale = type(current)(**{**current.__dict__, "status": OrchestratorTaskState.PENDING})
        stale = type(current)(**{**stale.__dict__, "version": 1})
        async with rt.db.transaction() as tx:
            dropped = await rt.repo.transition_task(
                tx, stale, OrchestratorTaskState.FAILED, now=utcnow()
            )
        async with rt.db.transaction() as tx:
            noop = await rt.repo.transition_task(
                tx, stale, OrchestratorTaskState.COMPLETED, now=utcnow()
            )
        events = await timeline(client, inv.id)
        after = (await detail(client, inv.id)).tasks[0]
    assert dropped is Applied.DROPPED and noop is Applied.NOOP
    assert after.status == "COMPLETED"
    [note] = [e.payload for e in events if e.type == "transition.dropped"]
    assert note["entity"] == "task" and note["current_state"] == "COMPLETED"
    assert note["attempted_state"] == "FAILED"
    assert len(task_states(events)) == 4  # neither stale write emitted a status event


async def test_llm_mode_column_is_added_to_an_s1_database(tmp_path) -> None:
    path = tmp_path / "s1.sqlite"
    db = Database(path)
    await db.open()
    async with db.transaction() as tx:
        await tx.execute("ALTER TABLE tasks DROP COLUMN llm_mode")
    cols = [r["name"] for r in await db.fetchall("PRAGMA table_info(tasks)")]
    assert "llm_mode" not in cols
    await db.close()

    again = Database(path)
    await again.open()
    try:
        cols = [r["name"] for r in await again.fetchall("PRAGMA table_info(tasks)")]
    finally:
        await again.close()
    assert cols[-1] == "llm_mode"
