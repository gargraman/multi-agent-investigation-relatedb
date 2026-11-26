"""Runner error mapping and deadline-free dedup, driven by the fake spec (S3a Task 4)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from fastapi import FastAPI

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_contracts.common import AgentId, LlmMode
from nexus_shared.agent.app import create_agent_app

from .conftest import make_request
from .test_runtime_fake_spec import (
    ORCH,
    FakeSpec,
    NullProvider,
    client,
    completed_with_batch,
    cs_accepting,
    drain,
    orchestrator_only,
    req,
    settings_for,
)


def _app(spec: FakeSpec, tmp_path: Path) -> FastAPI:
    return create_agent_app(
        spec,
        settings_for(tmp_path, service_name="geoint-agent"),
        token_registry=orchestrator_only(),
        provider=NullProvider(),
        context_transport=cs_accepting(),
    )


def _spec(error: Exception | None = None) -> FakeSpec:
    return FakeSpec(
        agent_id=AgentId.GEOINT,
        task_types=("analyze_synthetic_geospatial",),
        outcome=completed_with_batch(),
        error=error,
    )


async def test_unexpected_exception_ends_internal_error_not_agent_restarted(
    tmp_path: Path,
) -> None:
    app = _app(_spec(error=RuntimeError("boom")), tmp_path)
    async with client(app) as c:
        assert (await c.post("/v1/tasks", json=req(), headers=ORCH)).status_code == 202
        await drain(app)
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    assert body["status"] == "FAILED", body
    assert body["error"]["code"] == "internal_error"
    assert body["completed_at"] is not None


async def test_same_attempt_repost_with_new_deadline_is_200_duplicate(tmp_path: Path) -> None:
    spec = _spec()
    app = _app(spec, tmp_path)
    first = req()
    later = datetime.fromisoformat(first["deadline"]) + timedelta(seconds=90)
    repost = first | {"deadline": later.isoformat()}
    async with client(app) as c:
        assert (await c.post("/v1/tasks", json=first, headers=ORCH)).status_code == 202
        await drain(app)
        dup = await c.post("/v1/tasks", json=repost, headers=ORCH)
    assert dup.status_code == 200 and dup.json()["status"] == "COMPLETED"
    assert len(spec.runs) == 1


async def test_canceled_write_failure_is_handled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A cancel that interrupts ``spec.run`` and then cannot write CANCELED must not leak out of
    the asyncio task: the shared crash handler ends the row FAILED(internal_error)."""
    spec = _spec()
    spec.gates[1] = asyncio.Event()  # never released: the run blocks until the cancel lands
    app = _app(spec, tmp_path)
    request = make_request(agent="geoint-agent", task_id="tsk_1")
    async with client(app) as c:
        store, runner = app.state.store, app.state.runner
        await store.submit(request, llm_mode=LlmMode.REPLAY)
        real_update = store.update

        async def update(*args: Any, **fields: Any) -> Any:
            if fields.get("status") == AgentTaskStatus.CANCELED.value:
                raise aiosqlite.OperationalError("disk I/O error")
            return await real_update(*args, **fields)

        monkeypatch.setattr(store, "update", update)
        task = runner.start(request)
        await spec.started[1].wait()
        runner.cancel("tsk_1")
        await asyncio.wait([task])
        body = (await c.get("/v1/tasks/tsk_1", headers=ORCH)).json()
    assert not task.cancelled() and task.exception() is None
    assert body["status"] == "FAILED" and body["error"]["code"] == "internal_error", body
    assert any(r.message == "task crashed" for r in caplog.records)
