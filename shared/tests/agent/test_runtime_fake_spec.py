"""The shared runtime (app factory, API, runner) driven by a fake, non-SIGINT AgentSpec."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry import trace

from nexus_contracts.a2a.task import AgentTaskStatus, TaskRequest, TaskUsage
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.common import AgentId, Classification
from nexus_contracts.scenario.manifest import AgentManifest
from nexus_shared.agent.app import create_agent_app, lazy_app
from nexus_shared.agent.settings import AgentSettings
from nexus_shared.agent.spec import AgentSpec, PipelineOutcome
from nexus_shared.llm import LLMProvider, TokenBudget
from nexus_shared.security import TokenRegistry

from .conftest import AGENT_MANIFESTS, make_batch, make_request

ORCH = {"Authorization": "Bearer " + "o" * 40}


@dataclass
class FakeSpec:
    agent_id: AgentId
    task_types: tuple[str, ...]
    outcome: PipelineOutcome
    role: str = "Fake specialist used by the shared-runtime tests"
    version: str = "0.0.0-test"
    clearance: Classification = Classification.DEMO_MID
    runs: list[TaskRequest] = field(default_factory=list)
    # Concurrency hooks: ``started[attempt]`` is set when ``run`` begins; a run whose attempt has
    # an entry in ``gates`` waits on that event; ``error`` is raised instead of returning.
    started: defaultdict[int, asyncio.Event] = field(
        default_factory=lambda: defaultdict(asyncio.Event)
    )
    gates: dict[int, asyncio.Event] = field(default_factory=dict)
    error: Exception | None = None

    def validate_request(
        self, request: TaskRequest, manifest: AgentManifest | None
    ) -> ErrorCode | None:
        if request.task_type not in self.task_types or manifest is None:
            return ErrorCode.TASK_REJECTED
        return None

    async def run(
        self,
        request: TaskRequest,
        *,
        provider: LLMProvider,
        manifest: AgentManifest,
        fixtures_root: Path,
        budget: TokenBudget,
        tracer: trace.Tracer | None = None,
    ) -> PipelineOutcome:
        self.runs.append(request)
        self.started[request.attempt].set()
        gate = self.gates.get(request.attempt)
        if gate is not None:
            await gate.wait()
        if self.error is not None:
            raise self.error
        return self.outcome


class NullProvider:
    """A provider that must never be called by the fake spec."""

    async def generate_structured(self, **_: Any) -> Any:
        raise AssertionError("the fake spec makes no LLM call")


def completed_with_batch() -> PipelineOutcome:
    batch = make_batch(agent="geoint-agent").model_copy(update={"task_id": "tsk_1"})
    return PipelineOutcome(
        status=AgentTaskStatus.COMPLETED,
        batch=batch,
        usage=TaskUsage(input_tokens=10, output_tokens=5, llm_calls=1),
        model_id="fake-model",
        prompt_version="fake@1.0.0",
        error=None,
        summary="1 fake finding.",
    )


def settings_for(tmp_path: Path, *, service_name: str) -> AgentSettings:
    return AgentSettings(
        service_name=service_name,
        service_token="s" * 40,
        nexus_llm_mode="replay",
        sqlite_path=tmp_path / f"{service_name}.sqlite",
        agent_manifests_dir=AGENT_MANIFESTS,
        fixtures_dir=tmp_path / "fixtures",
        replay_dir=tmp_path / "replay",
        context_service_url="http://cs",
        context_backoff_seconds=0,
    )


def orchestrator_only() -> TokenRegistry:
    return TokenRegistry({"orchestrator": "o" * 40})


def cs_accepting() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        ids = [f"fnd_{i}" for i in range(len(payload["findings"]))]
        return httpx.Response(201, json={"task_id": payload["task_id"], "finding_ids": ids})

    return httpx.MockTransport(handler)


@asynccontextmanager
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as c,
    ):
        yield c


async def drain(app: FastAPI) -> None:
    await app.state.runner.drain()


def req(**kw: Any) -> dict[str, Any]:
    return make_request(agent="geoint-agent", task_id="tsk_1", **kw).model_dump(mode="json")


async def test_fake_spec_runs_end_to_end(tmp_path: Path) -> None:
    spec = FakeSpec(
        agent_id=AgentId.GEOINT,
        task_types=("analyze_synthetic_geospatial",),
        outcome=completed_with_batch(),
    )
    assert isinstance(spec, AgentSpec)
    app = create_agent_app(
        spec,
        settings_for(tmp_path, service_name="geoint-agent"),
        token_registry=orchestrator_only(),
        provider=NullProvider(),
        context_transport=cs_accepting(),
    )
    async with client(app) as c:
        assert (await c.post("/v1/tasks", json=req(), headers=ORCH)).status_code == 202
        await drain(app)
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
        assert body["status"] == "COMPLETED", body
        assert body["agent_id"] == "geoint-agent" and body["finding_ids"] == ["fnd_0"]
        assert body["prompt_version"] == "fake@1.0.0"
        card = (await c.get("/.well-known/agent-card.json")).json()
    assert card["id"] == "geoint-agent" and card["task_types"] == ["analyze_synthetic_geospatial"]
    assert card["role"] == spec.role
    assert card["clearance"] == spec.clearance.value == "DEMO-MID"
    assert len(spec.runs) == 1


async def test_fake_spec_policy_rejection_skips_run(tmp_path: Path) -> None:
    spec = FakeSpec(
        agent_id=AgentId.GEOINT,
        task_types=("analyze_synthetic_geospatial",),
        outcome=completed_with_batch(),
    )
    app = create_agent_app(
        spec,
        settings_for(tmp_path, service_name="geoint-agent"),
        token_registry=orchestrator_only(),
        provider=NullProvider(),
        context_transport=cs_accepting(),
    )
    async with client(app) as c:
        r = await c.post("/v1/tasks", json=req(task_type="analyze_other"), headers=ORCH)
        assert r.status_code == 202 and r.json()["status"] == "REJECTED"
        await drain(app)
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    assert body["status"] == "REJECTED" and body["agent_id"] == "geoint-agent"
    assert spec.runs == []


def test_lazy_app_builds_once_on_first_app_access(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus_shared.agent import app as app_module

    for var in ("SERVICE_TOKEN", "SQLITE_PATH", "NEXUS_TOKEN_ORCHESTRATOR"):
        monkeypatch.delenv(var, raising=False)
    spec = FakeSpec(
        agent_id=AgentId.GEOINT,
        task_types=("analyze_synthetic_geospatial",),
        outcome=completed_with_batch(),
    )

    class FakeSettings(AgentSettings):
        service_name: str = "fake-agent"

    logged: list[str] = []
    monkeypatch.setattr(app_module, "configure_logging", lambda name, level: logged.append(name))
    getattr_ = lazy_app(spec, FakeSettings)  # no env vars needed to build the hook
    assert logged == []

    with pytest.raises(AttributeError):
        getattr_("not_app")

    monkeypatch.setenv("SERVICE_TOKEN", "s" * 40)
    monkeypatch.setenv("SQLITE_PATH", ":memory:")
    monkeypatch.setenv("NEXUS_LLM_MODE", "replay")
    monkeypatch.setenv("NEXUS_TOKEN_ORCHESTRATOR", "o" * 40)
    first = getattr_("app")
    assert isinstance(first, FastAPI) and first.state.spec is spec
    assert getattr_("app") is first  # cached
    assert logged == ["fake-agent"]
