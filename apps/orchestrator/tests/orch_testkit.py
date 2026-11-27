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
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_contracts.common import AgentId, Classification, LlmMode
from nexus_contracts.events.timeline import TimelineEvent
from nexus_orchestrator.api.runtime import Runtime
from nexus_orchestrator.clock import Clock
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.main import create_app
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.repository import TaskRecord
from nexus_shared.telemetry import build_tracer_provider
from nexus_shared.testing import InMemorySpanExporter

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = REPO_ROOT / "data" / "scenarios"
TOKEN = "orchestrator-test-token-" + "x" * 16
SCENARIO = "baseline-corroboration"


class FakeClock:
    """Virtual time for the orchestrator's :class:`Clock`.

    ``sleep(s)`` blocks until virtual time reaches ``now + s`` (``s <= 0`` just yields). Time only
    moves when the test calls ``advance`` or ``run_for``, so a driver sleeping on this clock cannot
    push time forward by itself and deadline tests stay deterministic.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start
        self._waiters: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = 0

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        entry = (self._now + timedelta(seconds=seconds), self._seq, future)
        self._waiters.append(entry)
        try:
            await future
        finally:
            if entry in self._waiters:  # cancelled before it was woken
                self._waiters.remove(entry)

    def pending(self) -> int:
        """Number of sleepers still waiting."""
        return sum(1 for _, _, f in self._waiters if not f.done())

    def advance(self, seconds: float) -> None:
        """Move time forward and wake every sleeper whose target has been reached, in order."""
        self._now += timedelta(seconds=seconds)
        due = sorted((w for w in self._waiters if w[0] <= self._now), key=lambda w: (w[0], w[1]))
        for entry in due:
            self._waiters.remove(entry)
            if not entry[2].done():
                entry[2].set_result(None)

    async def run_for(self, seconds: float, step: float | None = None) -> None:
        """Advance ``seconds`` in steps, letting woken tasks run between steps. The default step
        is the smallest pending sleep (so a woken driver can re-sleep before time moves on), or
        ``seconds`` when nothing is sleeping."""
        end = self._now + timedelta(seconds=seconds)
        while self._now < end:
            remaining = (end - self._now).total_seconds()
            if step is not None:
                delta = step
            else:
                targets = [t for t, _, f in self._waiters if not f.done()]
                delta = (min(targets) - self._now).total_seconds() if targets else seconds
            self.advance(min(max(delta, 1e-6), remaining))
            for _ in range(5):
                await asyncio.sleep(0)
        await asyncio.sleep(0)


class FakeContextService:
    """HTTP test double for the Context Service (spec §12.3 ``POST /v1/investigations``).

    ``POST /v1/investigations/{id}/close`` records ``(id, status)`` in ``closes`` and answers 200;
    ``close_hangs = True`` makes it never answer (a Context Service that is up but stuck).
    ``journal``, when set, gets ``"close"`` appended per close call, so a test can check the
    order against the agents' cancels (give both doubles the same list). ``create_gate`` (an
    ``asyncio.Event``) holds the registration POST until set; ``log`` records processing order.
    """

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.requests: list[httpx.Request] = []
        self.registered: dict[str, dict] = {}
        self.closes: list[tuple[str, str]] = []
        self.close_hangs = False
        self.journal: list[str] | None = None
        self.log: list[str] = []  # "create:<id>" / "close:<id>", in the order they were processed
        self.create_gate: asyncio.Event | None = None  # holds POST /v1/investigations until set
        self.create_entered = 0  # creates that reached the double (before the gate)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("context-service down (test double)", request=request)
        self.requests.append(request)
        if request.url.path == "/readyz":
            return httpx.Response(200, json={"status": "ready"})
        if request.method == "POST" and request.url.path == "/v1/investigations":
            body = json.loads(request.content)
            self.log.append(f"create:{body['investigation_id']}")
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
        if request.method == "POST" and request.url.path.endswith("/close"):
            inv_id = request.url.path.split("/")[3]
            status = json.loads(request.content)["status"]
            self.closes.append((inv_id, status))
            self.log.append(f"close:{inv_id}")
            if self.journal is not None:
                self.journal.append("close")
            closed_at = utcnow().isoformat()
            return httpx.Response(
                200, json={"investigation_id": inv_id, "status": status, "closed_at": closed_at}
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

    async def _serve(self, request: httpx.Request) -> httpx.Response:
        if self.close_hangs and request.url.path.endswith("/close"):
            await asyncio.Event().wait()  # until the caller's timeout cancels us
        if (
            self.up
            and request.method == "POST"
            and request.url.path == "/v1/investigations"
            and self.create_gate is not None
        ):
            self.create_entered += 1
            await self.create_gate.wait()
        return self.handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._serve)

    def creates(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/v1/investigations"]


S = AgentTaskStatus


class FakeAgent:
    """HTTP test double for a specialist agent (spec §12.2): card, submit, poll.

    - ``POST /v1/tasks``: 202 for a new ``(task_id, attempt)``; 200 with the **current** status
      for a byte-identical duplicate (S2 G2); 409 ``content_conflict`` if the body differs.
    - ``GET /v1/tasks/{id}``: walks ``script`` one step per poll (``hold`` freezes it);
      404 for unknown ids (``forget()`` simulates an agent that lost its state).
    - ``POST /v1/tasks/{id}/cancel``: records the id in ``cancels``; the scripted status is left
      alone (the orchestrator has already moved on, which is what the tests look at).
      ``submit_gate`` (an ``asyncio.Event``) holds ``POST /v1/tasks`` until set (a submit that is
      in flight while something else happens); ``log`` records submit/cancel landing order.
      ``poll_gate`` / ``ready_gate`` do the same for ``GET /v1/tasks/{id}`` and ``GET /readyz``
      (a frozen agent that accepts the connection and never answers).
      ``hang_cancel = True`` makes it never answer. ``journal``, when set, gets
      ``"cancel:<task_id>"`` appended per call (share one list with ``FakeContextService``).
    - ``GET /readyz``: 200, or 503 while ``ready = False`` (the re-dispatch gate, FR-121).
    - ``reply_attempt`` makes poll results claim another attempt (a late reply from attempt 1).
    - ``up = False`` makes every request a connect error; ``lose_response = True`` lets the
      agent process the request, then drops the reply (a read error), as after a network cut.

    Chaos knobs (spec §12, ``tests/chaos``), all over the fields above:

    - ``hang()``: acknowledge, then report WORKING forever (an agent that never answers).
    - ``restart()``: the next poll of a task that is still open answers
      ``FAILED(agent_restarted)``, as the agent's boot sweep would; later attempts are unaffected.
    - ``down()`` / ``bring_up()``: ``up = False`` / ``True`` (every request, so submit, poll and
      ``/readyz`` alike, is a connect error while down). Not ``up()``: that name is the field.
    - ``answer_after(seconds, clock)``: reports WORKING until ``seconds`` of ``clock`` time have
      passed since the task was acknowledged, then COMPLETED.
    """

    def __init__(
        self,
        agent_id: AgentId = AgentId.SIGINT,
        *,
        mode: LlmMode = LlmMode.LIVE,
        script: list[AgentTaskStatus] | None = None,
        submit_status: AgentTaskStatus = S.SUBMITTED,
        fail_code: ErrorCode = ErrorCode.LLM_UNAVAILABLE,
        usage: tuple[int, int] = (120, 30),
    ) -> None:
        self.agent_id = agent_id
        self.mode = mode
        self.script = list(script if script is not None else [S.WORKING, S.COMPLETED])
        self.submit_status = submit_status
        self.fail_code = fail_code
        self.usage = usage  # (input, output) tokens reported with a terminal status
        self.ready = True  # ``GET /readyz``: 200 when True, 503 when False
        self.readyz_hits = 0
        self.reply_attempt: int | None = None  # report this attempt instead of the task's own
        self.up = True
        self.lose_response = False
        self.hold = False
        self.tasks: dict[str, dict] = {}
        self.submits: list[tuple[int, TaskRequest, httpx.Headers]] = []
        self.polls: list[str] = []
        self.cancels: list[str] = []  # task ids of every ``POST /v1/tasks/{id}/cancel``
        self.hang_cancel = False
        self.journal: list[str] | None = None
        self.log: list[str] = []  # "submit:<id>" / "cancel:<id>", in the order they landed
        self.submit_gate: asyncio.Event | None = None  # holds POST /v1/tasks until set
        self.submit_entered = 0  # submits that reached the agent (before the gate)
        self.poll_gate: asyncio.Event | None = None  # holds GET /v1/tasks/{id} until set
        self.poll_entered = 0
        self.ready_gate: asyncio.Event | None = None  # holds GET /readyz until set
        self.ready_entered = 0
        self.card_reads = 0
        self.restart_pending = False
        self.answer_clock: FakeClock | None = None
        self.answer_delay = 0.0
        self.app = self._build()

    def forget(self) -> None:
        self.tasks.clear()

    def hang(self) -> None:
        self.script = [S.WORKING]
        for task in self.tasks.values():  # a task already past WORKING must not index past it
            task["step"] = min(task["step"], 1)

    def restart(self) -> None:
        self.restart_pending = True

    def down(self) -> None:
        self.up = False

    def bring_up(self) -> None:
        self.up = True

    def answer_after(self, seconds: float, clock: FakeClock) -> None:
        self.answer_clock, self.answer_delay = clock, seconds

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
            attempt=self.reply_attempt if self.reply_attempt is not None else task["attempt"],
            llm_mode=self.mode,
            finding_ids=[f"fnd_{suffix}a", f"fnd_{suffix}b"] if status is S.COMPLETED else [],
            usage=(
                TaskUsage(
                    input_tokens=self.usage[0],
                    output_tokens=self.usage[1],
                    llm_calls=2,
                    replayed=self.mode is LlmMode.REPLAY,
                )
                if terminal
                else None
            ),
            model_id="fake-model-1" if terminal else None,
            completed_at=utcnow() if terminal else None,
            error=(
                TaskError.for_code(task.get("error") or self.fail_code)
                if status is S.FAILED
                else TaskError.for_code(ErrorCode.TASK_REJECTED)
                if status is S.REJECTED
                else None
            ),
        )

    def _build(self) -> FastAPI:
        app = FastAPI()
        fake = self

        @app.get("/readyz")
        async def readyz() -> JSONResponse:
            fake.readyz_hits += 1
            if fake.ready_gate is not None:
                fake.ready_entered += 1
                await fake.ready_gate.wait()
            code = 200 if fake.ready else 503
            return JSONResponse({"status": "ready" if fake.ready else "starting"}, status_code=code)

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
            if fake.submit_gate is not None:
                fake.submit_entered += 1
                await fake.submit_gate.wait()
            fake.log.append(f"submit:{body.task_id}")
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
                "error": None,
                "answer_at": (
                    fake.answer_clock.now() + timedelta(seconds=fake.answer_delay)
                    if fake.answer_clock is not None
                    else None
                ),
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
            if fake.poll_gate is not None:
                fake.poll_entered += 1
                await fake.poll_gate.wait()
            task = fake.tasks.get(task_id)
            if task is None:
                return JSONResponse({"title": "Not Found", "status": 404}, status_code=404)
            if fake.submit_status.is_terminal:
                task["status"] = fake.submit_status
            elif fake._current(task).is_terminal:
                pass  # already answered: later polls repeat it
            elif fake.restart_pending:
                fake.restart_pending = False
                task["status"], task["error"] = S.FAILED, ErrorCode.AGENT_RESTARTED
            elif task["answer_at"] is not None:
                done = fake.answer_clock.now() >= task["answer_at"]
                task["status"] = S.COMPLETED if done else S.WORKING
            elif not fake.hold and task["step"] < len(fake.script):
                task["step"] += 1
            return JSONResponse(fake._result(task_id, task).model_dump(mode="json"))

        @app.post("/v1/tasks/{task_id}/cancel")
        async def cancel(task_id: str, request: Request) -> JSONResponse:
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                return JSONResponse({"title": "Unauthorized", "status": 401}, status_code=401)
            fake.cancels.append(task_id)
            fake.log.append(f"cancel:{task_id}")
            if fake.journal is not None:
                fake.journal.append(f"cancel:{task_id}")
            if fake.hang_cancel:
                await asyncio.Event().wait()  # until the caller's timeout cancels us
            if task_id not in fake.tasks:
                return JSONResponse({"title": "Not Found", "status": 404}, status_code=404)
            return JSONResponse({"task_id": task_id, "status": S.CANCELED.value})

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


class Router(httpx.AsyncBaseTransport):
    """Routes by host to each agent's transport and logs ``(host, method, path)`` in order."""

    def __init__(self, agents: dict[str, FakeAgent]) -> None:
        self.agents = agents
        self.transports = {host: a.transport for host, a in agents.items()}
        self.log: list[tuple[str, str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.log.append((host, request.method, request.url.path))
        return await self.transports[host].handle_async_request(request)


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


async def create(client) -> str:
    resp = await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def the_task(app, inv_id: str) -> TaskRecord:
    [task] = await runtime(app).repo.task_records(inv_id)
    return task


async def events(client, inv_id: str, type_: str) -> list[TimelineEvent]:
    resp = await client.get(f"/api/v1/investigations/{inv_id}/timeline", params={"limit": 100})
    return [TimelineEvent.model_validate(e) for e in resp.json()["items"] if e["type"] == type_]


async def parked(clock: FakeClock) -> None:
    """Wait until the driver has finished its tick and is sleeping on the clock again. The close
    reconciler (FR-126) always holds one sleeper of its own, so the driver is the second."""

    async def sleeping() -> bool:
        return clock.pending() >= 2

    await wait_until(sleeping)


def task_in(app, inv_id: str, *states: T) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await the_task(app, inv_id)).status in states

    return check


async def pump(
    clock: FakeClock, predicate: Callable[[], Awaitable[bool]], step: float = 0.01
) -> None:
    """Advance virtual time in small steps (real pauses between them for the DB threads) until
    ``predicate`` holds."""
    end = time.monotonic() + 5.0
    while not await predicate():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        clock.advance(step)
        await asyncio.sleep(0.005)


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
