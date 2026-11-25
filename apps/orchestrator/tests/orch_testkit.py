"""Orchestrator test kit (helpers + fixtures, re-exported by conftest.py).

The Context Service is replaced by ``FakeContextService``, an ``httpx.MockTransport`` handler.
It is a **test double at the HTTP boundary**: it checks what we send, not the real service's
behaviour. The real orchestrator <-> Context Service integration runs in Compose.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from nexus_orchestrator.api.runtime import Runtime
from nexus_orchestrator.main import create_app
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_shared.telemetry import build_tracer_provider
from nexus_shared.testing import InMemorySpanExporter

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = REPO_ROOT / "data" / "scenarios"
TOKEN = "orchestrator-test-token-" + "x" * 16
SCENARIO = "baseline-corroboration"


class FakeContextService:
    """HTTP test double for the Context Service (spec §12.3 ``POST /v1/investigations``)."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.requests: list[httpx.Request] = []
        self.registered: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("context-service down (test double)", request=request)
        self.requests.append(request)
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        if request.method == "POST" and request.url.path == "/v1/investigations":
            body = json.loads(request.content)
            created = body["investigation_id"] not in self.registered
            self.registered[body["investigation_id"]] = body
            return httpx.Response(
                200,
                json={
                    "investigation_id": body["investigation_id"],
                    "created": created,
                    "entity_count": len(body["entities"]),
                    "evidence_count": len(body["evidence"]),
                },
            )
        return httpx.Response(404, json={"title": "Not Found", "status": 404})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def creates(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/v1/investigations"]


@pytest.fixture
def make_settings(tmp_path: Path):
    def _make(**overrides: object) -> OrchestratorSettings:
        values: dict[str, object] = {
            "service_token": TOKEN,
            "sqlite_path": tmp_path / "orchestrator.sqlite",
            "context_service_url": "http://context-service.test",
            "scenarios_dir": SCENARIOS_DIR,
            "nexus_max_concurrent_investigations": 10,
            "leader_heartbeat_seconds": 0.05,
            "leader_stale_seconds": 0.3,
            "leader_wait_seconds": 0.3,
            "leader_poll_seconds": 0.02,
            "context_retry_interval_seconds": 0.05,
            "sse_heartbeat_seconds": 0.2,
            "context_service_timeout_seconds": 1.0,
            "health_probe_timeout_seconds": 0.5,
        }
        values.update(overrides)
        return OrchestratorSettings(**values)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def make_app(make_settings, exporter):
    def _make(fake: FakeContextService, **overrides: object) -> FastAPI:
        settings = make_settings(**overrides)
        provider = build_tracer_provider(settings, exporter)
        return create_app(settings, fake.transport, tracer_provider=provider)

    return _make


def runtime(app: FastAPI) -> Runtime:
    return app.state.runtime


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the lifespan and give an in-process client (no SSE: ASGITransport buffers bodies)."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://orch.test") as client:
            yield client


@asynccontextmanager
async def served(app: FastAPI) -> AsyncIterator[str]:
    """Run the app under a real uvicorn server on a free port (needed for streaming SSE)."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started or task.done():
            break
        await asyncio.sleep(0.01)
    if task.done():
        task.result()
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
