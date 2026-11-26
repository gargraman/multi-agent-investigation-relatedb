from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nexus_shared.security import TokenRegistry
from nexus_shared.testing import InMemorySpanExporter
from nexus_sigint.main import create_app
from nexus_sigint.settings import SigintSettings

from .conftest import FIXTURES, SCENARIOS, FakeProvider, make_request, valid_output

ORCH = {"Authorization": "Bearer " + "o" * 40}


class ContextStub:
    """``POST /v1/findings:batch`` at the HTTP boundary (httpx.MockTransport)."""

    def __init__(self, status: int = 201, body: dict | None = None) -> None:
        self.status = status
        self.body = body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = json.loads(request.content)
        body = self.body or {
            "task_id": payload["task_id"],
            "finding_ids": [f"fnd_{i}" for i in range(len(payload["findings"]))],
        }
        return httpx.Response(self.status, json=body)


def build(tmp_path: Path, provider: Any, stub: ContextStub) -> tuple[FastAPI, InMemorySpanExporter]:
    settings = SigintSettings(
        service_token="s" * 40,
        nexus_llm_mode="replay",
        sqlite_path=tmp_path / "sigint.sqlite",
        scenarios_dir=SCENARIOS,
        fixtures_dir=FIXTURES,
        replay_dir=tmp_path / "replay",
        context_service_url="http://cs",
        context_backoff_seconds=0,
    )
    exporter = InMemorySpanExporter()
    app = create_app(
        settings,
        token_registry=TokenRegistry({"orchestrator": "o" * 40}),
        provider=provider,
        context_transport=httpx.MockTransport(stub),
        span_exporter=exporter,
    )
    return app, exporter


@pytest.fixture
def stub() -> ContextStub:
    return ContextStub()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider(valid_output(2), valid_output(2))


@pytest.fixture
async def env(tmp_path: Path, provider: FakeProvider, stub: ContextStub) -> AsyncIterator[Any]:
    app, exporter = build(tmp_path, provider, stub)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://sigint") as c,
    ):
        yield app, c, exporter


def body(**kw: Any) -> dict[str, Any]:
    return make_request(**kw).model_dump(mode="json")


async def test_submit_run_and_poll(env, stub: ContextStub) -> None:
    app, c, exporter = env
    r = await c.post("/v1/tasks", json=body(), headers=ORCH)
    assert r.status_code == 202 and r.json() == {"task_id": "tsk_test1", "status": "SUBMITTED"}
    await app.state.runner.drain()
    r = await c.get("/v1/tasks/tsk_test1", headers=ORCH)
    res = r.json()
    assert r.status_code == 200, res
    assert res["status"] == "COMPLETED" and res["finding_ids"] == ["fnd_0", "fnd_1"]
    assert res["llm_mode"] == "replay" and res["usage"]["llm_calls"] == 1
    assert res["prompt_version"] == "sigint-classify@1.0.0" and res["agent_id"] == "sigint-agent"
    assert len(stub.requests) == 1
    assert stub.requests[0].headers["Authorization"] == "Bearer " + "s" * 40
    assert stub.requests[0].headers.get("traceparent")
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert {"task.run", "detector.recurring_contacts", "gen_ai.generate"} <= set(spans)
    # task.run continues the orchestrator's trace from TaskRequest.trace_context
    assert format(spans["task.run"].context.trace_id, "032x") == "1" * 32
    assert spans["gen_ai.generate"].parent.span_id == spans["task.run"].context.span_id


async def test_duplicate_conflict_stale_unknown(env) -> None:
    app, c, _ = env
    second = body(attempt=2)
    assert (await c.post("/v1/tasks", json=second, headers=ORCH)).status_code == 202
    await app.state.runner.drain()
    dup = await c.post("/v1/tasks", json=second, headers=ORCH)
    assert dup.status_code == 200 and dup.json()["status"] == "COMPLETED"
    retry = second | {"trace_context": {"traceparent": "00-" + "3" * 32 + "-" + "4" * 16 + "-01"}}
    again = await c.post("/v1/tasks", json=retry, headers=ORCH)  # orchestrator retry, new trace
    assert again.status_code == 200 and again.json()["status"] == "COMPLETED"
    conflict = await c.post("/v1/tasks", json=body(attempt=2, token_budget=9999), headers=ORCH)
    assert conflict.status_code == 409 and conflict.json()["code"] == "content_conflict"
    assert conflict.headers["content-type"].startswith("application/problem+json")
    stale = await c.post("/v1/tasks", json=body(attempt=1), headers=ORCH)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_attempt"
    assert (await c.get("/v1/tasks/tsk_nope", headers=ORCH)).status_code == 404


async def test_auth_and_public_endpoints(env) -> None:
    _, c, _ = env
    assert (await c.post("/v1/tasks", json=body())).status_code == 401
    bad = {"Authorization": "Bearer " + "x" * 40}
    r = await c.get("/v1/tasks/tsk_test1", headers=bad)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    card = (await c.get("/.well-known/agent-card.json")).json()
    assert card["id"] == "sigint-agent" and card["llm_mode"] == "replay"
    assert card["task_types"] == ["analyze_synthetic_communications"]
    assert card["clearance"] == "DEMO-HIGH"
    ready = await c.get("/readyz")
    assert ready.status_code == 200 and ready.json()["llm_mode"] == "replay"
    assert (await c.get("/livez")).json() == {"status": "ok"}


async def test_policy_rejection_is_recorded(env, provider: FakeProvider) -> None:
    app, c, _ = env
    r = await c.post("/v1/tasks", json=body(input_refs=["ev-geo-001"]), headers=ORCH)
    assert r.status_code == 202 and r.json()["status"] == "REJECTED"
    await app.state.runner.drain()
    res = (await c.get("/v1/tasks/tsk_test1", headers=ORCH)).json()
    assert res["status"] == "REJECTED" and res["error"]["code"] == "task_rejected"
    assert provider.calls == []


async def test_malformed_body_is_problem_422(env) -> None:
    _, c, _ = env
    r = await c.post("/v1/tasks", json={"task_id": "tsk_x"}, headers=ORCH)
    assert r.status_code == 422 and r.json()["code"] == "schema_invalid"


@pytest.mark.parametrize(
    ("stub", "status", "code", "ids"),
    [
        (ContextStub(422, {"title": "x", "status": 422}), "FAILED", "submission_rejected", []),
        (
            ContextStub(
                409,
                {
                    "title": "Conflict",
                    "status": 409,
                    "code": "already_committed",
                    "finding_ids": ["fnd_old"],
                },
            ),
            "COMPLETED",
            None,
            ["fnd_old"],
        ),
        (ContextStub(503, {"title": "x", "status": 503}), "FAILED", "context_unavailable", []),
    ],
)
async def test_submission_outcomes(env, status: str, code: str | None, ids: list[str]) -> None:
    app, c, _ = env
    await c.post("/v1/tasks", json=body(), headers=ORCH)
    await app.state.runner.drain()
    res = (await c.get("/v1/tasks/tsk_test1", headers=ORCH)).json()
    assert res["status"] == status and res["finding_ids"] == ids
    assert (res.get("error") or {}).get("code") == code


@pytest.mark.parametrize("provider", [FakeProvider("bad", "bad")])
async def test_pipeline_failure_reaches_task_result(env) -> None:
    app, c, _ = env
    await c.post("/v1/tasks", json=body(), headers=ORCH)
    await app.state.runner.drain()
    res = (await c.get("/v1/tasks/tsk_test1", headers=ORCH)).json()
    assert res["status"] == "FAILED" and res["error"]["code"] == "schema_invalid"
    assert res["usage"]["llm_calls"] == 2 and res["finding_ids"] == []


async def test_deadline_already_passed(env, provider: FakeProvider) -> None:
    app, c, _ = env
    await c.post("/v1/tasks", json=body(deadline="2020-01-01T00:00:00Z"), headers=ORCH)
    await app.state.runner.drain()
    res = (await c.get("/v1/tasks/tsk_test1", headers=ORCH)).json()
    assert res["status"] == "FAILED" and res["error"]["code"] == "deadline_exceeded"
    assert provider.calls == []
