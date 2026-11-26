"""S3a: two specialists end to end in process (AC3-b) and the agent contract for both (AC4-c).

AC3-b drives the real orchestrator against two ``FakeAgent`` doubles (SIGINT and GEOINT) behind
one host-routing transport. AC4-c runs the S2 agent HTTP contract (submit/poll, duplicate 200,
conflict 409, stale 409, cancel) against the **real** ``create_app`` of both nodes, with a
scripted provider and a Context Service stub at the HTTP boundary. Node packages are imported
only by this test module, never by orchestrator code (packaging rule, spec §7).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from orch_testkit import (
    SCENARIO,
    SCENARIOS_DIR,
    FakeAgent,
    FakeContextService,
    Router,
    S,
    running,
    wait_until,
)
from pydantic import BaseModel

from nexus_contracts.a2a.task import TaskRequest, TaskUsage
from nexus_contracts.api.investigations import Investigation
from nexus_contracts.common import AgentId, LlmMode
from nexus_contracts.events.timeline import TimelineEvent
from nexus_contracts.scenario.loader import load_manifest
from nexus_geoint.main import create_app as create_geoint
from nexus_geoint.settings import GeointSettings
from nexus_orchestrator.main import create_app as create_orchestrator
from nexus_shared.llm import LLMResult
from nexus_shared.llm.schema import parse_output
from nexus_shared.security import TokenRegistry
from nexus_shared.telemetry import build_tracer_provider
from nexus_sigint.main import create_app as create_sigint
from nexus_sigint.settings import SigintSettings

TERMINAL = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}
URLS = {"sigint-agent": "http://sigint.test", "geoint-agent": "http://geoint.test"}
FIXTURES_DIR = SCENARIOS_DIR.parent / "fixtures"


# -- AC3-b: one investigation, both specialists, one trace -------------------------------------


def _trace_id(traceparent: str) -> str:
    return traceparent.split("-")[1]


async def test_both_specialists_dispatched_in_one_tick_under_one_trace(
    make_settings, exporter
) -> None:
    sig = FakeAgent(AgentId.SIGINT, mode=LlmMode.REPLAY, script=[S.WORKING, S.COMPLETED])
    geo = FakeAgent(AgentId.GEOINT, mode=LlmMode.REPLAY, script=[S.WORKING, S.COMPLETED])
    router = Router({"sigint.test": sig, "geoint.test": geo})
    settings = make_settings(
        nexus_enabled_agents="sigint-agent,geoint-agent", nexus_agent_urls=URLS
    )
    app = create_orchestrator(
        settings,
        FakeContextService().transport,
        agent_transport=router,
        tracer_provider=build_tracer_provider(settings, exporter),
    )
    async with running(app) as client:
        resp = await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO})
        assert resp.status_code == 201, resp.text
        inv_id = resp.json()["id"]

        async def finished() -> bool:
            body = (await client.get(f"/api/v1/investigations/{inv_id}")).json()
            return body["status"] in TERMINAL

        await wait_until(finished)
        done = Investigation.model_validate(
            (await client.get(f"/api/v1/investigations/{inv_id}")).json()
        )
        page = await client.get(f"/api/v1/investigations/{inv_id}/timeline", params={"limit": 100})
        events = [TimelineEvent.model_validate(e) for e in page.json()["items"]]

    # Both receive a TaskRequest in the same dispatch tick: both POSTs precede the first poll.
    calls = [(host, method) for host, method, path in router.log if path.startswith("/v1/tasks")]
    first_poll = next(i for i, (_, method) in enumerate(calls) if method == "GET")
    assert sorted(h for h, m in calls[:first_poll] if m == "POST") == ["geoint.test", "sigint.test"]
    [(sig_code, sig_req, sig_headers)] = sig.submits
    [(geo_code, geo_req, geo_headers)] = geo.submits
    assert (sig_code, geo_code) == (202, 202)
    assert sig_req.task_type == "analyze_synthetic_communications"
    assert geo_req.task_type == "analyze_synthetic_geospatial"

    # One trace: body trace_context and HTTP traceparent of both requests share the trace id.
    trace_id = _trace_id(sig_req.trace_context.traceparent)
    assert _trace_id(geo_req.trace_context.traceparent) == trace_id
    assert _trace_id(sig_headers["traceparent"]) == trace_id
    assert _trace_id(geo_headers["traceparent"]) == trace_id

    # The exporter holds a dispatch span for each agent's task under that trace id.
    submits = [s for s in exporter.get_finished_spans() if s.name == "a2a.submit"]
    by_task = {s.attributes["nexus.task.id"]: s for s in submits}
    assert set(by_task) == {sig_req.task_id, geo_req.task_id}
    assert {format(s.context.trace_id, "032x") for s in by_task.values()} == {trace_id}

    # Analysis stays disabled: PARTIAL(agent_not_enabled), only analysis-agent skipped.
    assert done.status == "PARTIAL" and done.status_reason == "agent_not_enabled"
    assert {t.agent_id: t.status for t in done.tasks} == {
        AgentId.SIGINT: "COMPLETED",
        AgentId.GEOINT: "COMPLETED",
    }
    assert all(t.llm_mode is LlmMode.REPLAY for t in done.tasks)
    status_events = [e.payload for e in events if e.type == "investigation.status_changed"]
    running_event = next(p for p in status_events if p["to_status"] == "RUNNING")
    assert running_event["skipped_agents"] == ["analysis-agent"]
    assert status_events[-1]["to_status"] == "PARTIAL"
    assert status_events[-1]["reason"] == "agent_not_enabled"


# -- AC4-c: the S2 agent contract, for both real agent apps ------------------------------------

ORCH = {"Authorization": "Bearer " + "o" * 40}
LABELS = {AgentId.SIGINT: "RECURRING_CONTACT", AgentId.GEOINT: "CO_LOCATION"}


class LabellingProvider:
    """Provider double: labels every candidate in the input with the agent's pattern label."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0

    async def generate_structured(
        self, *, input_data: dict[str, Any], output_schema: type[BaseModel], **_: Any
    ) -> LLMResult:
        self.calls += 1
        out = {
            "classifications": [
                {
                    "candidate_id": c["candidate_id"],
                    "pattern_label": self.label,
                    "confidence": 0.8,
                    "statement": "Pattern observed in the structured records.",
                    "reasoning_summary": "Detector candidate with consistent records.",
                }
                for c in input_data["candidates"]
            ]
        }
        usage = TaskUsage(input_tokens=100, output_tokens=50, llm_calls=1)
        return LLMResult(
            output=parse_output(output_schema, out),
            usage=usage,
            model_id="fake-model",
            latency_ms=1,
        )


def _context_stub(requests: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        ids = [f"fnd_{i}" for i in range(len(payload["findings"]))]
        return httpx.Response(201, json={"task_id": payload["task_id"], "finding_ids": ids})

    return handler


def _request(agent: AgentId, **kw: Any) -> dict[str, Any]:
    manifest = load_manifest(SCENARIOS_DIR / SCENARIO)
    task = next(t for t in manifest.template.tasks if t.agent is agent)
    body: dict[str, Any] = {
        "task_id": "tsk_test1",
        "investigation_id": "inv_test1",
        "task_type": task.task_type,
        "objective": manifest.objective,
        "input_refs": list(task.input_refs),
        "scenario_id": SCENARIO,
        "classification_ceiling": "DEMO-HIGH",
        "attempt": 1,
        "token_budget": 15000,
        "deadline": (datetime.now(UTC) + timedelta(seconds=180)).isoformat(),
        "contract_version": "1.0",
        "trace_context": {"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"},
    }
    body.update(kw)
    return TaskRequest.model_validate(body).model_dump(mode="json")


def _agent_app(agent: AgentId, tmp_path: Path, cs_requests: list[httpx.Request]) -> FastAPI:
    factory, settings_cls = {
        AgentId.SIGINT: (create_sigint, SigintSettings),
        AgentId.GEOINT: (create_geoint, GeointSettings),
    }[agent]
    settings = settings_cls(
        service_token="s" * 40,
        nexus_llm_mode="replay",
        sqlite_path=tmp_path / f"{agent.value}.sqlite",
        scenarios_dir=SCENARIOS_DIR,
        fixtures_dir=FIXTURES_DIR,
        replay_dir=tmp_path / "replay",
        context_service_url="http://cs",
        context_backoff_seconds=0,
    )
    return factory(
        settings,
        token_registry=TokenRegistry({"orchestrator": "o" * 40}),
        provider=LabellingProvider(LABELS[agent]),
        context_transport=httpx.MockTransport(_context_stub(cs_requests)),
    )


@pytest.fixture(params=[AgentId.SIGINT, AgentId.GEOINT], ids=lambda a: a.value)
async def agent_env(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[tuple[AgentId, FastAPI, httpx.AsyncClient, list[httpx.Request]]]:
    agent: AgentId = request.param
    cs_requests: list[httpx.Request] = []
    app = _agent_app(agent, tmp_path, cs_requests)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as c,
    ):
        yield agent, app, c, cs_requests


async def test_contract_submit_and_poll(agent_env) -> None:
    agent, app, c, cs_requests = agent_env
    r = await c.post("/v1/tasks", json=_request(agent), headers=ORCH)
    assert r.status_code == 202 and r.json() == {"task_id": "tsk_test1", "status": "SUBMITTED"}
    await app.state.runner.drain()
    res = (await c.get("/v1/tasks/tsk_test1", headers=ORCH)).json()
    assert res["status"] == "COMPLETED", res
    assert res["agent_id"] == agent.value and res["llm_mode"] == "replay"
    assert res["attempt"] == 1 and res["finding_ids"]
    assert len(cs_requests) == 1
    assert json.loads(cs_requests[0].content)["task_id"] == "tsk_test1"
    assert (await c.post("/v1/tasks", json=_request(agent))).status_code == 401
    assert (await c.get("/v1/tasks/tsk_nope", headers=ORCH)).status_code == 404


async def test_contract_duplicate_conflict_stale(agent_env) -> None:
    agent, app, c, _ = agent_env
    second = _request(agent, attempt=2)
    assert (await c.post("/v1/tasks", json=second, headers=ORCH)).status_code == 202
    await app.state.runner.drain()
    dup = await c.post("/v1/tasks", json=second, headers=ORCH)
    assert dup.status_code == 200 and dup.json() == {"task_id": "tsk_test1", "status": "COMPLETED"}
    retry = second | {"trace_context": {"traceparent": "00-" + "3" * 32 + "-" + "4" * 16 + "-01"}}
    again = await c.post("/v1/tasks", json=retry, headers=ORCH)  # new trace: still a duplicate
    assert again.status_code == 200 and again.json()["status"] == "COMPLETED"
    conflict = await c.post(
        "/v1/tasks", json=_request(agent, attempt=2, token_budget=9999), headers=ORCH
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "content_conflict"
    assert conflict.headers["content-type"].startswith("application/problem+json")
    stale = await c.post("/v1/tasks", json=_request(agent, attempt=1), headers=ORCH)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_attempt"


async def test_contract_cancel_is_idempotent(agent_env) -> None:
    agent, app, c, _ = agent_env
    await c.post("/v1/tasks", json=_request(agent), headers=ORCH)
    await app.state.runner.drain()
    for _ in range(2):
        r = await c.post("/v1/tasks/tsk_test1/cancel", headers=ORCH)
        assert r.status_code == 200
        assert r.json() == {"task_id": "tsk_test1", "status": "COMPLETED"}
    unknown = await c.post("/v1/tasks/tsk_nope/cancel", headers=ORCH)
    assert unknown.status_code == 404
    assert (await c.post("/v1/tasks/tsk_test1/cancel")).status_code == 401
    res = (await c.get("/v1/tasks/tsk_test1", headers=ORCH)).json()
    assert res["status"] == "COMPLETED"
