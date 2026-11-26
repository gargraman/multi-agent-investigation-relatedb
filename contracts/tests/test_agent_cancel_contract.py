"""Agent cancel wire contract (S3 FR-102-106): ``POST /v1/tasks/{id}/cancel``.

The response body is a ``TaskCancelResponse``.

Drives the in-process shared agent app (the same app SIGINT and GEOINT run) with a slow pipeline
held on an ``asyncio.Event``, so there is no timing assumption: submit, wait until the pipeline
is RUNNING (task status ``WORKING``), cancel, then release the pipeline. The cancel answers 200
with a body that validates as ``TaskCancelResponse`` (the status at the moment of the cancel, still
``WORKING``), the task settles ``CANCELED``, a repeated cancel is the idempotent 200 ``CANCELED``,
and the agent never makes its Context Service commit.

The detailed runner behaviour (cancel during commit, supersession, ...) is covered in
``shared/tests/agent/test_cancel.py``; this file pins only the contract a caller relies on.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from nexus_contracts.a2a.task import (
    AgentTaskStatus,
    TaskCancelResponse,
    TaskRequest,
    TaskResult,
    TaskUsage,
)
from nexus_contracts.common import AgentId, Classification
from nexus_contracts.scenario import load_manifest
from nexus_shared.agent.app import create_agent_app
from nexus_shared.agent.settings import AgentSettings
from nexus_shared.agent.spec import PipelineOutcome
from nexus_shared.security import TokenRegistry

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "data" / "scenarios"
SCENARIO = "baseline-corroboration"
TOKEN = "o" * 40
ORCH = {"Authorization": f"Bearer {TOKEN}"}
TASK_ID = "tsk_cancel_contract"


class SlowSpec:
    """A GEOINT-shaped spec whose pipeline blocks on ``release``, then returns an outcome."""

    agent_id = AgentId.GEOINT
    task_types = ("analyze_synthetic_geospatial",)
    role = "Slow specialist for the cancel contract test"
    version = "0.0.0-test"

    def __init__(self) -> None:
        self.clearance = Classification.DEMO_MID
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def validate_request(self, request: TaskRequest, manifest: Any) -> None:
        return None

    async def run(self, request: TaskRequest, **_: Any) -> PipelineOutcome:
        self.started.set()
        await self.release.wait()
        return PipelineOutcome(
            status=AgentTaskStatus.COMPLETED,
            batch=None,
            usage=TaskUsage(input_tokens=1, output_tokens=1, llm_calls=1),
            model_id="slow-model",
            prompt_version="slow@1.0.0",
            error=None,
            summary="never reported",
        )


class Unreachable:
    """Context Service stand-in: records every request (the agent must make none)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(500)


def _request() -> dict[str, Any]:
    manifest = load_manifest(SCENARIOS / SCENARIO)
    task = next(t for t in manifest.template.tasks if t.agent is AgentId.GEOINT)
    return TaskRequest.model_validate(
        {
            "task_id": TASK_ID,
            "investigation_id": "inv_cancel_contract",
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
    ).model_dump(mode="json")


async def test_cancel_while_running_answers_200_then_settles_canceled(tmp_path: Path) -> None:
    spec = SlowSpec()
    cs = Unreachable()
    settings = AgentSettings(
        service_name="geoint-agent",
        service_token="s" * 40,
        nexus_llm_mode="replay",
        sqlite_path=tmp_path / "geoint-agent.sqlite",
        scenarios_dir=SCENARIOS,
        fixtures_dir=tmp_path / "fixtures",
        replay_dir=tmp_path / "replay",
        context_service_url="http://cs",
        context_backoff_seconds=0,
    )
    app = create_agent_app(
        spec,
        settings,
        token_registry=TokenRegistry({"orchestrator": TOKEN}),
        context_transport=httpx.MockTransport(cs.handler),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as c,
    ):
        submit = await c.post("/v1/tasks", json=_request(), headers=ORCH)
        assert submit.status_code == 202
        await spec.started.wait()  # the slow pipeline is running
        running = TaskResult.model_validate(
            (await c.get(f"/v1/tasks/{TASK_ID}", headers=ORCH)).json()
        )
        assert running.status is AgentTaskStatus.WORKING

        cancel = await c.post(f"/v1/tasks/{TASK_ID}/cancel", headers=ORCH)
        assert cancel.status_code == 200
        body = TaskCancelResponse.model_validate(cancel.json())
        assert body.task_id == TASK_ID
        assert body.status is AgentTaskStatus.WORKING  # the cancel is a request: it lands async

        # The pipeline finishes only now. The cancel flag is checked before the commit, so the
        # agent ends CANCELED and never talks to the Context Service.
        spec.release.set()
        await app.state.runner.drain()
        final = TaskResult.model_validate(
            (await c.get(f"/v1/tasks/{TASK_ID}", headers=ORCH)).json()
        )
        again = await c.post(f"/v1/tasks/{TASK_ID}/cancel", headers=ORCH)

    assert final.status is AgentTaskStatus.CANCELED
    assert final.error is None and final.completed_at is not None
    assert again.status_code == 200
    assert TaskCancelResponse.model_validate(again.json()).status is AgentTaskStatus.CANCELED
    assert cs.requests == [], "a canceled task must never commit findings"
