"""Agent-side cancel and attempt supersession (S3 FR-102–106, §8.3; S3a Task 4).

Every concurrency step is gated on an ``asyncio.Event``: no timing assumptions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_contracts.common import AgentId, LlmMode
from nexus_shared.agent.app import create_agent_app
from nexus_shared.security import TokenRegistry

from .conftest import make_request
from .test_runtime_fake_spec import (
    ORCH,
    FakeSpec,
    NullProvider,
    client,
    completed_with_batch,
    drain,
    orchestrator_only,
    req,
    settings_for,
)


class RecordingCS:
    """Context Service stub: records every batch; with ``block`` it waits for ``release``; with
    ``cancelled`` it answers 409 ``investigation_cancelled``, else 201."""

    def __init__(self, *, block: bool = False, cancelled: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.answered = False
        self.cancelled = cancelled
        if not block:
            self.release.set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.entered.set()
        await self.release.wait()
        self.answered = True
        if self.cancelled:
            return httpx.Response(
                409, json={"title": "Conflict", "status": 409, "code": "investigation_cancelled"}
            )
        ids = [f"fnd_{i}" for i in range(len(payload["findings"]))]
        return httpx.Response(201, json={"task_id": payload["task_id"], "finding_ids": ids})


def _spec(gates: dict[int, asyncio.Event] | None = None) -> FakeSpec:
    return FakeSpec(
        agent_id=AgentId.GEOINT,
        task_types=("analyze_synthetic_geospatial",),
        outcome=completed_with_batch(),
        gates=gates or {},
    )


def _app(
    spec: FakeSpec, tmp_path: Path, cs: RecordingCS, registry: TokenRegistry | None = None
) -> FastAPI:
    return create_agent_app(
        spec,
        settings_for(tmp_path, service_name="geoint-agent"),
        token_registry=registry or orchestrator_only(),
        provider=NullProvider(),
        context_transport=httpx.MockTransport(cs.handler),
    )


async def test_cancel_before_llm_call_ends_canceled(tmp_path: Path) -> None:
    spec = _spec({1: asyncio.Event()})  # never released: the run blocks "in the LLM call"
    cs = RecordingCS()
    app = _app(spec, tmp_path, cs)
    async with client(app) as c:
        assert (await c.post("/v1/tasks", json=req(), headers=ORCH)).status_code == 202
        await spec.started[1].wait()
        r = await c.post("/v1/tasks/tsk_1/cancel", headers=ORCH)
        assert r.status_code == 200 and r.json() == {"task_id": "tsk_1", "status": "WORKING"}
        await drain(app)
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    assert body["status"] == "CANCELED", body
    assert body["error"] is None and body["completed_at"] is not None
    assert cs.requests == []


async def test_cancel_requested_before_start_skips_run(tmp_path: Path) -> None:
    spec = _spec()
    cs = RecordingCS()
    app = _app(spec, tmp_path, cs)
    request = make_request(agent="geoint-agent", task_id="tsk_1")
    async with client(app) as c:
        await app.state.store.submit(request, llm_mode=LlmMode.REPLAY)
        await app.state.store.request_cancel("tsk_1")
        app.state.runner.start(request)
        await drain(app)
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    assert body["status"] == "CANCELED", body
    assert spec.runs == [] and cs.requests == []


async def test_cancel_flag_checked_before_commit(tmp_path: Path) -> None:
    gate = asyncio.Event()
    spec = _spec({1: gate})
    cs = RecordingCS()
    app = _app(spec, tmp_path, cs)
    async with client(app) as c:
        await c.post("/v1/tasks", json=req(), headers=ORCH)
        await spec.started[1].wait()
        await app.state.store.request_cancel("tsk_1")  # flag only: the run is not interrupted
        gate.set()
        await drain(app)
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    assert body["status"] == "CANCELED", body
    assert body["usage"]["llm_calls"] == 1  # the finished run's usage is kept
    assert cs.requests == []


async def test_cancel_after_terminal_is_idempotent_200_with_current_status(
    tmp_path: Path,
) -> None:
    app = _app(_spec(), tmp_path, RecordingCS())
    async with client(app) as c:
        await c.post("/v1/tasks", json=req(), headers=ORCH)
        await drain(app)
        for _ in range(2):
            r = await c.post("/v1/tasks/tsk_1/cancel", headers=ORCH)
            assert r.status_code == 200
            assert r.json() == {"task_id": "tsk_1", "status": "COMPLETED"}
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
        flagged = await app.state.store.is_cancel_requested("tsk_1", 1)
    assert body["status"] == "COMPLETED" and not flagged


async def test_cancel_unknown_task_404(tmp_path: Path) -> None:
    app = _app(_spec(), tmp_path, RecordingCS())
    async with client(app) as c:
        r = await c.post("/v1/tasks/tsk_nope/cancel", headers=ORCH)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_cancel_requires_orchestrator_token(tmp_path: Path) -> None:
    registry = TokenRegistry({"orchestrator": "o" * 40, "context-service": "c" * 40})
    app = _app(_spec(), tmp_path, RecordingCS(), registry)
    async with client(app) as c:
        await c.post("/v1/tasks", json=req(), headers=ORCH)
        await drain(app)
        none = await c.post("/v1/tasks/tsk_1/cancel")
        other = await c.post(
            "/v1/tasks/tsk_1/cancel", headers={"Authorization": "Bearer " + "c" * 40}
        )
        bad = await c.post("/v1/tasks/tsk_1/cancel", headers={"Authorization": "Bearer nope"})
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    for r in (none, other, bad):
        assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
    assert body["status"] == "COMPLETED"


async def test_cancel_during_commit_does_not_interrupt_and_409_cancelled_maps_to_canceled(
    tmp_path: Path,
) -> None:
    cs = RecordingCS(block=True, cancelled=True)
    app = _app(_spec(), tmp_path, cs)
    async with client(app) as c:
        await c.post("/v1/tasks", json=req(), headers=ORCH)
        await cs.entered.wait()  # the commit is in flight
        r = await c.post("/v1/tasks/tsk_1/cancel", headers=ORCH)
        assert r.status_code == 200 and r.json()["status"] == "WORKING"
        await asyncio.sleep(0)  # let a (wrong) interrupt land before the commit is released
        cs.release.set()
        await drain(app)
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    assert cs.answered, "the in-flight commit was interrupted"
    assert len(cs.requests) == 1
    assert body["status"] == "CANCELED", body
    assert body["error"] is None


async def test_409_cancelled_without_cancel_request_maps_to_canceled(tmp_path: Path) -> None:
    app = _app(_spec(), tmp_path, RecordingCS(cancelled=True))
    async with client(app) as c:
        await c.post("/v1/tasks", json=req(), headers=ORCH)
        await drain(app)
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    assert body["status"] == "CANCELED", body


async def test_higher_attempt_cancels_running_lower_attempt(tmp_path: Path) -> None:
    spec = _spec({1: asyncio.Event()})  # attempt 1 blocks forever; attempt 2 is not gated
    cs = RecordingCS()
    app = _app(spec, tmp_path, cs)
    async with client(app) as c:
        assert (await c.post("/v1/tasks", json=req(), headers=ORCH)).status_code == 202
        await spec.started[1].wait()
        assert (await c.post("/v1/tasks", json=req(attempt=2), headers=ORCH)).status_code == 202
        await drain(app)
        latest = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
        lower = await app.state.store.get("tsk_1", 1)
    assert lower.status is AgentTaskStatus.CANCELED
    assert latest["attempt"] == 2 and latest["status"] == "COMPLETED", latest
    assert [r.attempt for r in spec.runs] == [1, 2]
    assert len(cs.requests) == 1


async def test_shutdown_leaves_row_working_for_boot_sweep(tmp_path: Path) -> None:
    spec = _spec({1: asyncio.Event()})
    app = _app(spec, tmp_path, RecordingCS())
    async with client(app) as c:
        await c.post("/v1/tasks", json=req(), headers=ORCH)
        await spec.started[1].wait()
        await app.state.runner.stop()
        row = await app.state.store.get("tsk_1", 1)
    assert row.status is AgentTaskStatus.WORKING


async def test_cancel_submitted_task_endpoint(tmp_path: Path) -> None:
    """Characterization: a cancel that lands before the runner's first step ends CANCELED without
    ever running the pipeline, and the terminal status is stable on later reads."""
    spec = _spec()
    cs = RecordingCS()
    app = _app(spec, tmp_path, cs)
    async with client(app) as c:
        assert (await c.post("/v1/tasks", json=req(), headers=ORCH)).status_code == 202
        r = await c.post("/v1/tasks/tsk_1/cancel", headers=ORCH)  # no await point since the submit
        assert r.status_code == 200 and r.json()["task_id"] == "tsk_1"
        await drain(app)
        first = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
        again = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    assert first["status"] == again["status"] == "CANCELED", first
    assert first["error"] is None and first["completed_at"] is not None
    assert spec.runs == [] and cs.requests == []


async def test_rejected_attempt2_supersedes_running_attempt1(tmp_path: Path) -> None:
    """S3a T4 ruling, relied on by the orchestrator's re-dispatch: a higher attempt that
    ``validate_request`` rejects still supersedes the running lower one, and GET reports it."""
    spec = _spec({1: asyncio.Event()})  # attempt 1 blocks forever
    cs = RecordingCS()
    app = _app(spec, tmp_path, cs)
    async with client(app) as c:
        assert (await c.post("/v1/tasks", json=req(), headers=ORCH)).status_code == 202
        await spec.started[1].wait()
        second = await c.post(
            "/v1/tasks", json=req(attempt=2, task_type="analyze_other"), headers=ORCH
        )
        assert second.status_code == 202 and second.json()["status"] == "REJECTED"
        await drain(app)  # attempt 1 was interrupted, so this returns
        latest = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
        lower = await app.state.store.get("tsk_1", 1)
    assert lower.status is AgentTaskStatus.CANCELED
    assert latest["attempt"] == 2 and latest["status"] == "REJECTED", latest
    assert [r.attempt for r in spec.runs] == [1]  # attempt 2 never ran
    assert cs.requests == []  # no batch committed
