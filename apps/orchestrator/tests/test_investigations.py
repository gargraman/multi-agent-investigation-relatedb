"""Create / read / timeline / resume behaviour (spec §8.1 steps 1-3, §8.6, §12.1; ADR-002 A1)."""

from __future__ import annotations

import asyncio
import json

import pytest
from orch_testkit import SCENARIO, SCENARIOS_DIR, TOKEN, FakeContextService, running, runtime

from nexus_contracts.api.context import ContextInvestigationCreate
from nexus_contracts.api.investigations import Investigation, InvestigationPage
from nexus_contracts.api.problem import ProblemDetails
from nexus_contracts.events.timeline import TimelinePage
from nexus_contracts.scenario.loader import load_manifest

PROBLEM_JSON = "application/problem+json"


async def create(client, scenario: str = SCENARIO, **extra):
    return await client.post("/api/v1/investigations", json={"scenario_id": scenario, **extra})


async def test_create_returns_201_running_with_task_and_timeline_rows(make_app) -> None:
    """S2: registration succeeded, so the same request moved it to RUNNING with one SIGINT
    task (the only enabled template agent); GEOINT and Analysis are listed as skipped."""
    fake = FakeContextService()
    app = make_app(fake)
    async with running(app) as client:
        rt = runtime(app)
        resp = await create(client)
        assert resp.status_code == 201, resp.text
        inv = Investigation.model_validate(resp.json())
        assert inv.status == "RUNNING"
        [task] = inv.tasks
        assert task.agent_id == "sigint-agent" and task.attempt == 1
        assert task.status in {"PENDING", "DISPATCHED"}  # the driver may already have run
        assert inv.token_budget == 60000
        manifest = load_manifest(SCENARIOS_DIR / SCENARIO)
        assert inv.objective == manifest.objective

        timeline = TimelinePage.model_validate(
            (await client.get(f"/api/v1/investigations/{inv.id}/timeline")).json()
        )
        created, running_ev, pending = timeline.items[:3]
        assert created.type == running_ev.type == "investigation.status_changed"
        assert created.payload == {
            "from_status": None,
            "to_status": "CREATED",
            "reason": None,
            "skipped_agents": [],
        }
        assert running_ev.payload == {
            "from_status": "CREATED",
            "to_status": "RUNNING",
            "reason": None,
            "skipped_agents": ["geoint-agent", "analysis-agent"],
        }
        assert pending.type == "task.status_changed"
        assert pending.payload["from_state"] is None
        assert pending.payload["to_state"] == "PENDING"
        assert created.seq < running_ev.seq < pending.seq

        detail = Investigation.model_validate(
            (await client.get(f"/api/v1/investigations/{inv.id}")).json()
        )
        assert detail.id == inv.id and detail.status == "RUNNING"
        assert rt.workflow.pending_registration == set()


async def test_event_published_only_after_commit(make_app, monkeypatch) -> None:
    fake = FakeContextService()
    app = make_app(fake)
    async with running(app) as client:
        rt = runtime(app)
        published: list = []
        monkeypatch.setattr(rt.broker, "publish", published.append)

        async def failing_commit(conn) -> None:
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(rt.db, "_commit", failing_commit)
        resp = await create(client)
        assert resp.status_code == 500
        assert resp.headers["content-type"] == PROBLEM_JSON
        assert published == []  # nothing reached SSE
        assert fake.creates() == []  # and the Context Service was not called
        monkeypatch.undo()

        page = InvestigationPage.model_validate((await client.get("/api/v1/investigations")).json())
        assert page.items == []  # the insert was rolled back

        # Sanity: with a working commit the same path does publish.
        published.clear()
        monkeypatch.setattr(rt.broker, "publish", published.append)
        assert (await create(client)).status_code == 201
        assert published[0].payload["to_status"] == "CREATED"
        seqs = [e.seq for e in published]
        assert seqs == sorted(set(seqs))  # each committed event published once, in order


async def test_outbound_create_has_traceparent_token_and_manifest_body(make_app, exporter) -> None:
    fake = FakeContextService()
    app = make_app(fake)
    async with running(app) as client:
        resp = await create(client, objective="Custom objective")
        inv = Investigation.model_validate(resp.json())

    [req] = fake.creates()
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    traceparent = req.headers["traceparent"]
    manifest = load_manifest(SCENARIOS_DIR / SCENARIO)
    expected = ContextInvestigationCreate.from_manifest(inv.id, manifest, "Custom objective")
    assert json.loads(req.content) == expected.model_dump(mode="json")

    spans = {s.name: s for s in exporter.get_finished_spans()}
    create_span = spans["workflow.create"]
    trace_id = format(create_span.context.trace_id, "032x")
    assert traceparent.split("-")[1] == trace_id  # same trace end to end
    assert create_span.attributes["nexus.investigation.id"] == inv.id

    # trace_context_json is stored on the investigation and points at workflow.create.
    app2_db = runtime(app).db
    await app2_db.open()
    try:
        row = await app2_db.fetchone(
            "SELECT trace_context_json FROM investigations WHERE id = ?", (inv.id,)
        )
    finally:
        await app2_db.close()
    stored = json.loads(row["trace_context_json"])
    assert stored["traceparent"].split("-")[1] == trace_id
    assert stored["traceparent"].split("-")[2] == format(create_span.context.span_id, "016x")


async def test_context_service_down_stays_created_then_sweep_registers(make_app) -> None:
    fake = FakeContextService(up=False)
    app = make_app(fake, context_retry_interval_seconds=60)
    async with running(app) as client:
        resp = await create(client)
        assert resp.status_code == 201
        inv_id = resp.json()["id"]
        assert runtime(app).workflow.pending_registration == {inv_id}
        detail = (await client.get(f"/api/v1/investigations/{inv_id}")).json()
        assert detail["status"] == "CREATED"
    assert fake.registered == {}

    # Restart with the Context Service up: the startup sweep re-POSTs the idempotent create.
    fake.up = True
    app2 = make_app(fake)
    async with running(app2) as client:
        assert inv_id in fake.registered
        detail = (await client.get(f"/api/v1/investigations/{inv_id}")).json()
        assert detail["status"] == "RUNNING"  # S2: registration chains into dispatch
        assert [t["agent_id"] for t in detail["tasks"]] == ["sigint-agent"]
        assert runtime(app2).workflow.pending_registration == set()
        assert (await client.get("/readyz")).status_code == 200


async def test_background_retrier_registers_when_context_service_returns(make_app) -> None:
    fake = FakeContextService(up=False)
    app = make_app(fake)
    async with running(app) as client:
        inv_id = (await create(client)).json()["id"]
        fake.up = True
        for _ in range(100):
            if inv_id in fake.registered:
                break
            await asyncio.sleep(0.02)
        assert inv_id in fake.registered
        assert runtime(app).workflow.pending_registration == set()


async def test_startup_never_fails_when_context_service_down(make_app) -> None:
    fake = FakeContextService(up=False)
    app = make_app(fake, context_retry_interval_seconds=60)
    async with running(app) as client:
        await create(client)
    app2 = make_app(fake, context_retry_interval_seconds=60)
    async with running(app2) as client:
        assert (await client.get("/readyz")).status_code == 200
        assert len(runtime(app2).workflow.pending_registration) == 1


async def test_unknown_scenario_is_404_problem(make_app) -> None:
    app = make_app(FakeContextService())
    async with running(app) as client:
        resp = await create(client, scenario="no-such-scenario")
        assert resp.status_code == 404
        assert resp.headers["content-type"] == PROBLEM_JSON
        problem = ProblemDetails.model_validate(resp.json())
        assert problem.status == 404 and "no-such-scenario" in (problem.detail or "")


async def test_schema_invalid_is_422_with_errors(make_app) -> None:
    app = make_app(FakeContextService())
    async with running(app) as client:
        resp = await client.post(
            "/api/v1/investigations", json={"scenario_id": SCENARIO, "bogus": 1}
        )
        assert resp.status_code == 422
        assert resp.headers["content-type"] == PROBLEM_JSON
        problem = ProblemDetails.model_validate(resp.json())
        assert problem.code == "schema_invalid"
        assert problem.errors and problem.errors[0].loc == ["body", "bogus"]

        bad_cursor = await client.get("/api/v1/investigations", params={"cursor": "!!"})
        assert bad_cursor.status_code == 422
        assert bad_cursor.json()["code"] == "schema_invalid"

        missing = await client.get("/api/v1/investigations/inv_DOESNOTEXIST")
        assert missing.status_code == 404
        assert missing.headers["content-type"] == PROBLEM_JSON


async def test_concurrency_cap_is_429(make_app) -> None:
    fake = FakeContextService()
    app = make_app(fake, nexus_max_concurrent_investigations=1)
    async with running(app) as client:
        assert (await create(client)).status_code == 201
        resp = await create(client)
        assert resp.status_code == 429
        assert resp.headers["content-type"] == PROBLEM_JSON
        assert resp.headers["retry-after"] == "30"
    assert len(fake.creates()) == 1


async def test_list_pagination_newest_first(make_app) -> None:
    app = make_app(FakeContextService())
    async with running(app) as client:
        ids = [(await create(client)).json()["id"] for _ in range(3)]
        p1 = InvestigationPage.model_validate(
            (await client.get("/api/v1/investigations", params={"limit": 2})).json()
        )
        assert [i.id for i in p1.items] == ids[::-1][:2]
        assert p1.next_cursor
        p2 = InvestigationPage.model_validate(
            (
                await client.get(
                    "/api/v1/investigations", params={"limit": 2, "cursor": p1.next_cursor}
                )
            ).json()
        )
        assert [i.id for i in p2.items] == [ids[0]]
        assert p2.next_cursor is None


async def test_scenarios_list_exposes_metadata_only(make_app) -> None:
    app = make_app(FakeContextService())
    async with running(app) as client:
        body = (await client.get("/api/v1/scenarios")).json()
    by_id = {s["id"]: s for s in body["items"]}
    assert {"baseline-corroboration", "contradiction", "prompt-injection"} <= set(by_id)
    s = by_id[SCENARIO]
    assert set(s) == {"id", "version", "objective", "task_count"}
    assert s["task_count"] == 3 and s["version"] == "1.0.0"


@pytest.mark.parametrize("up,expected", [(True, "OK"), (False, "DEGRADED")])
async def test_system_health(make_app, up: bool, expected: str) -> None:
    fake = FakeContextService(up=up)
    app = make_app(fake)
    async with running(app) as client:
        body = (await client.get("/api/v1/system/health")).json()
        assert (await client.get("/livez")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200
    assert body["status"] == expected
    comps = {c["name"]: c["status"] for c in body["components"]}
    assert comps == {
        "orchestrator": "OK",
        "context-service": "OK" if up else "DOWN",
        "sigint-agent": "OK",
    }
    [agent] = [c for c in body["components"] if c["name"] == "sigint-agent"]
    assert agent["llm_mode"] == "live"
