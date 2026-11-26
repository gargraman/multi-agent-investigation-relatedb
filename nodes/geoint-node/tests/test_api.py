from __future__ import annotations

import json
from pathlib import Path

import httpx

from nexus_geoint.main import create_app
from nexus_geoint.settings import GeointSettings
from nexus_shared.security import TokenRegistry
from nexus_shared.testing import InMemorySpanExporter

from .conftest import FIXTURES, SCENARIOS, ScriptedProvider, make_request, valid_output_for

ORCH = {"Authorization": "Bearer " + "o" * 40}


def test_geoint_identity() -> None:
    ok = GeointSettings(service_token="s" * 40, nexus_llm_mode="replay")
    assert ok.service_name == "geoint-agent" and ok.sqlite_path == Path("var/geoint.sqlite")


async def test_api_card_and_submit(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def context_stub(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        ids = [f"fnd_{i}" for i in range(len(payload["findings"]))]
        return httpx.Response(201, json={"task_id": payload["task_id"], "finding_ids": ids})

    settings = GeointSettings(
        service_token="s" * 40,
        nexus_llm_mode="replay",
        sqlite_path=tmp_path / "geoint.sqlite",
        scenarios_dir=SCENARIOS,
        fixtures_dir=FIXTURES,
        replay_dir=tmp_path / "replay",
        context_service_url="http://cs",
        context_backoff_seconds=0,
    )
    exporter = InMemorySpanExporter()
    provider = ScriptedProvider(valid_output_for)
    app = create_app(
        settings,
        token_registry=TokenRegistry({"orchestrator": "o" * 40}),
        provider=provider,
        context_transport=httpx.MockTransport(context_stub),
        span_exporter=exporter,
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://geoint") as c,
    ):
        card = (await c.get("/.well-known/agent-card.json")).json()
        assert card["id"] == "geoint-agent" and card["llm_mode"] == "replay"
        assert card["task_types"] == ["analyze_synthetic_geospatial"]
        assert card["clearance"] == "DEMO-MID"
        assert (await c.post("/v1/tasks", json={})).status_code == 401

        body = make_request().model_dump(mode="json")
        r = await c.post("/v1/tasks", json=body, headers=ORCH)
        assert r.status_code == 202 and r.json()["status"] == "SUBMITTED"
        await app.state.runner.drain()
        res = (await c.get("/v1/tasks/tsk_test1", headers=ORCH)).json()
    assert res["status"] == "COMPLETED", res
    assert res["agent_id"] == "geoint-agent"
    assert res["prompt_version"] == "geoint-classify@1.0.0"
    assert res["finding_ids"] == ["fnd_0", "fnd_1", "fnd_2"]
    assert len(requests) == 1
    sent = json.loads(requests[0].content)
    assert {a["type"] for f in sent["findings"] for a in f["assertions"]} == {"PLACED_AT"}
    spans = {s.name for s in exporter.get_finished_spans()}
    assert {"task.run", "detector.placement_candidates", "gen_ai.generate"} <= spans
