"""Orchestrator test kit (helpers + fixtures, re-exported by conftest.py).

The Context Service is replaced by ``FakeContextService``, an ``httpx.MockTransport`` handler.
Agents are replaced by ``FakeAgent``, an in-process FastAPI app served through
``httpx.ASGITransport``. Both are **test doubles at the HTTP boundary**: they check what we send
and replay a scripted agent, not the real services' behaviour. The real orchestrator <-> agent /
Context Service integration runs in Compose (WP-E).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nexus_contracts.a2a.task import (
    AgentCard,
    AgentTaskStatus,
    TaskError,
    TaskRequest,
    TaskResult,
    TaskUsage,
)
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.common import AgentId, Classification, LlmMode
from nexus_orchestrator.api.runtime import Runtime
from nexus_orchestrator.clock import Clock
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.main import create_app
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_shared.telemetry import build_tracer_provider
from nexus_shared.testing import InMemorySpanExporter

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = REPO_ROOT / "data" / "scenarios"
TOKEN = "orchestrator-test-token-" + "x" * 16
SCENARIO = "baseline-corroboration"


class FakeClock:
    """Virtual time for the orchestrator's :class:`Clock`. ``sleep`` advances time instead of
    waiting, then yields once so other tasks run; ``advance`` moves it from the test."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


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
        if request.method == "GET" and request.url.path.endswith("/context"):
            inv_id = request.url.path.split("/")[3]
            if self.context_status != 200:
                return httpx.Response(self.context_status, json={"title": "boom"})
            if inv_id not in self.registered:
                return httpx.Response(404, json={"title": "Not Found", "status": 404})
            return httpx.Response(
                200, json={"investigation_id": inv_id, "status": "RUNNING", "findings": []}
            )
        return httpx.Response(404, json={"title": "Not Found", "status": 404})

    context_status = 200  # force a non-200 answer on GET /v1/investigations/{id}/context

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def creates(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/v1/investigations"]


S = AgentTaskStatus


class FakeAgent:
    """HTTP test double for a specialist agent (spec §12.2): card, submit, poll.

    - ``POST /v1/tasks``: 202 for a new ``(task_id, attempt)``; 200 with the **current** status
      for a byte-identical duplicate (S2 G2); 409 ``content_conflict`` if the body differs.
    - ``GET /v1/tasks/{id}``: walks ``script`` one step per poll (``hold`` freezes it);
      404 for unknown ids (``forget()`` simulates an agent that lost its state).
    - ``up = False`` makes every request a connect error; ``lose_response = True`` lets the
      agent process the request, then drops the reply (a read error), as after a network cut.
    """

    def __init__(
        self,
        agent_id: AgentId = AgentId.SIGINT,
        *,
        mode: LlmMode = LlmMode.LIVE,
        script: list[AgentTaskStatus] | None = None,
        submit_status: AgentTaskStatus = S.SUBMITTED,
        fail_code: ErrorCode = ErrorCode.LLM_UNAVAILABLE,
    ) -> None:
        self.agent_id = agent_id
        self.mode = mode
        self.script = list(script if script is not None else [S.WORKING, S.COMPLETED])
        self.submit_status = submit_status
        self.fail_code = fail_code
        self.up = True
        self.lose_response = False
        self.hold = False
        self.tasks: dict[str, dict] = {}
        self.submits: list[tuple[int, TaskRequest, httpx.Headers]] = []
        self.polls: list[str] = []
        self.card_reads = 0
        self.app = self._build()

    def forget(self) -> None:
        self.tasks.clear()

    def statuses(self) -> list[int]:
        return [code for code, _, _ in self.submits]

    @property
    def transport(self) -> httpx.AsyncBaseTransport:
        return _Switchable(self, httpx.ASGITransport(app=self.app))

    def _current(self, task: dict) -> AgentTaskStatus:
        if task["status"] is not None:
            return task["status"]
        return self.script[task["step"] - 1] if task["step"] else self.submit_status

    def _result(self, task_id: str, task: dict) -> TaskResult:
        status = self._current(task)
        terminal = status.is_terminal
        suffix = task_id.removeprefix("tsk_")
        return TaskResult(
            task_id=task_id,
            agent_id=self.agent_id,
            status=status,
            attempt=task["attempt"],
            llm_mode=self.mode,
            finding_ids=[f"fnd_{suffix}a", f"fnd_{suffix}b"] if status is S.COMPLETED else [],
            usage=(
                TaskUsage(
                    input_tokens=120,
                    output_tokens=30,
                    llm_calls=2,
                    replayed=self.mode is LlmMode.REPLAY,
                )
                if terminal
                else None
            ),
            model_id="fake-model-1" if terminal else None,
            completed_at=utcnow() if terminal else None,
            error=(
                TaskError.for_code(self.fail_code)
                if status is S.FAILED
                else TaskError.for_code(ErrorCode.TASK_REJECTED)
                if status is S.REJECTED
                else None
            ),
        )

    def _build(self) -> FastAPI:
        app = FastAPI()
        fake = self

        @app.get("/.well-known/agent-card.json")
        async def card() -> dict:
            fake.card_reads += 1
            return AgentCard(
                id=fake.agent_id,
                role="test double",
                task_types=["analyze_synthetic_communications"],
                version="test",
                clearance=Classification.DEMO_HIGH,
                llm_mode=fake.mode,
            ).model_dump(mode="json")

        @app.post("/v1/tasks")
        async def submit(request: Request) -> JSONResponse:
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                return JSONResponse({"title": "Unauthorized", "status": 401}, status_code=401)
            raw = await request.body()
            body = TaskRequest.model_validate_json(raw)
            digest = hashlib.sha256(
                json.dumps(json.loads(raw), sort_keys=True).encode()
            ).hexdigest()
            task = fake.tasks.get(body.task_id)
            if task is not None and task["attempt"] == body.attempt:
                if task["hash"] != digest:
                    fake.submits.append((409, body, request.headers))
                    return JSONResponse(
                        {"title": "Conflict", "status": 409, "code": "content_conflict"},
                        status_code=409,
                    )
                fake.submits.append((200, body, request.headers))
                status = fake._current(task)
                return JSONResponse({"task_id": body.task_id, "status": status.value})
            fake.tasks[body.task_id] = {
                "attempt": body.attempt,
                "hash": digest,
                "step": 0,
                "status": None,
            }
            fake.submits.append((202, body, request.headers))
            return JSONResponse(
                {"task_id": body.task_id, "status": fake.submit_status.value}, status_code=202
            )

        @app.get("/v1/tasks/{task_id}")
        async def poll(task_id: str, request: Request) -> JSONResponse:
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                return JSONResponse({"title": "Unauthorized", "status": 401}, status_code=401)
            fake.polls.append(task_id)
            task = fake.tasks.get(task_id)
            if task is None:
                return JSONResponse({"title": "Not Found", "status": 404}, status_code=404)
            if fake.submit_status.is_terminal:
                task["status"] = fake.submit_status
            elif not fake.hold and task["step"] < len(fake.script):
                task["step"] += 1
            return JSONResponse(fake._result(task_id, task).model_dump(mode="json"))

        return app


class _Switchable(httpx.AsyncBaseTransport):
    """Delegates to the agent's ASGI app; a connect error while ``fake.up`` is False."""

    def __init__(self, fake: FakeAgent, inner: httpx.AsyncBaseTransport) -> None:
        self.fake = fake
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.fake.up:
            raise httpx.ConnectError("agent down (test double)", request=request)
        response = await self.inner.handle_async_request(request)
        if self.fake.lose_response:
            await response.aread()
            raise httpx.ReadError("response lost (test double)", request=request)
        return response


async def wait_until(
    predicate: Callable[[], Awaitable[bool]], timeout: float = 5.0, interval: float = 0.02
) -> None:
    """Poll an async predicate until it is true; fail the test on timeout."""
    end = time.monotonic() + timeout
    while True:
        if await predicate():
            return
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


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
            "poll_interval_seconds": 0.02,
            "poll_jitter_ratio": 0.0,
            "dispatch_backoff_seconds": 0.01,
            "dispatch_timeout_seconds": 1.0,
            "poll_connect_timeout_seconds": 0.5,
            "poll_read_timeout_seconds": 1.0,
            "agent_card_refresh_seconds": 0.05,
        }
        values.update(overrides)
        return OrchestratorSettings(**values)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def make_app(make_settings, exporter):
    def _make(
        fake: FakeContextService,
        agent: FakeAgent | None = None,
        *,
        clock: Clock | None = None,
        **overrides: object,
    ) -> FastAPI:
        """``agent`` defaults to a SIGINT double that never finishes (held at SUBMITTED), so
        S1-style tests see a stable RUNNING investigation."""
        settings = make_settings(**overrides)
        provider = build_tracer_provider(settings, exporter)
        if agent is None:
            agent = FakeAgent()
            agent.hold = True
        return create_app(
            settings,
            fake.transport,
            agent_transport=agent.transport,
            tracer_provider=provider,
            clock=clock,
        )

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
