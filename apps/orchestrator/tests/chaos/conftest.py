"""Chaos harness (S3 spec §12, NFR-105): the real orchestrator against two ``FakeAgent`` doubles
(SIGINT, GEOINT), a ``FakeContextService`` and one ``FakeClock``, all in process.

``drive(seconds)`` is the only way time moves. It advances the clock in poll-interval steps and,
after each step, yields until every driver (and the close reconciler) is sleeping on the clock
again, so a tick's DB-backed effects have landed before the next step. No real sleeps beyond a
couple of milliseconds, no dependence on wall-clock speed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from orch_testkit import (
    FakeAgent,
    FakeClock,
    FakeContextService,
    Router,
    create,
    running,
    runtime,
)

from nexus_contracts.api.investigations import Investigation
from nexus_contracts.common import AgentId
from nexus_contracts.events.timeline import TimelineEvent
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.main import create_app
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.repository import TaskRecord
from nexus_shared.telemetry import build_tracer_provider

URLS = {"sigint-agent": "http://sigint.test", "geoint-agent": "http://geoint.test"}
POLL_SECONDS = 5.0  # one driver tick per 5 virtual seconds: 600 s of deadline is 120 steps


class Chaos:
    """One orchestrator deployment under test. ``app()`` builds a fresh process over the same DB
    file and the same fakes, which is how a restart is simulated."""

    def __init__(self, make_settings: Callable[..., OrchestratorSettings], exporter, **overrides):
        self.sig = FakeAgent(AgentId.SIGINT)
        self.geo = FakeAgent(AgentId.GEOINT)
        self.ctx = FakeContextService()
        self.clock = FakeClock(utcnow())
        self.settings = make_settings(
            nexus_enabled_agents="sigint-agent,geoint-agent",
            nexus_agent_urls=URLS,
            poll_interval_seconds=POLL_SECONDS,
            cancel_step_timeout_seconds=0.05,
            **overrides,
        )
        self.poll = POLL_SECONDS
        self._exporter = exporter

    def app(self) -> FastAPI:
        return create_app(
            self.settings,
            self.ctx.transport,
            agent_transport=Router({"sigint.test": self.sig, "geoint.test": self.geo}),
            tracer_provider=build_tracer_provider(self.settings, self._exporter),
            clock=self.clock,
        )

    @asynccontextmanager
    async def session(self, app: FastAPI | None = None) -> AsyncIterator[Session]:
        """Start an app (a new one by default) and give a handle to drive and inspect it."""
        app = app or self.app()
        async with running(app) as client:
            yield Session(self, app, client)


class Session:
    def __init__(self, chaos: Chaos, app: FastAPI, client: httpx.AsyncClient) -> None:
        self.chaos, self.app, self.client = chaos, app, client

    async def create(self) -> str:
        return await create(self.client)

    async def settle(self) -> None:
        """Wait until every active driver and the reconciler sleep on the clock, i.e. the tick
        that is in flight (DB writes, HTTP calls to the doubles) has finished."""
        clock, rt = self.chaos.clock, runtime(self.app)
        end = time.monotonic() + 5.0
        # Dispatch jobs run off the tick: also wait until each one is done or sleeping between
        # retry tries (those sleepers are on the clock too), and no driver is mid-tick.
        while (
            clock.pending() < 1 + len(rt.poller.active()) + rt.poller.dispatcher.backing_off
            or not rt.poller.quiescent()
        ):
            if time.monotonic() > end:
                raise AssertionError("drivers did not park")
            await asyncio.sleep(0.002)

    async def drive(self, seconds: float) -> None:
        """Advance virtual time by ``seconds`` in poll-interval steps, settling after each."""
        await self.settle()
        remaining = seconds
        while remaining > 0:
            step = min(POLL_SECONDS, remaining)
            self.chaos.clock.advance(step)
            remaining -= step
            await self.settle()

    async def drive_until_terminal(self, inv_id: str, limit: float = 700.0) -> Investigation:
        """Drive until the investigation is terminal (bounded by ``limit`` virtual seconds)."""
        spent = 0.0
        while spent < limit:
            inv = await self.detail(inv_id)
            if inv.status.is_terminal:
                return inv
            await self.drive(POLL_SECONDS)
            spent += POLL_SECONDS
        raise AssertionError(f"still {(await self.detail(inv_id)).status} after {limit} s")

    async def detail(self, inv_id: str) -> Investigation:
        resp = await self.client.get(f"/api/v1/investigations/{inv_id}")
        return Investigation.model_validate(resp.json())

    async def tasks(self, inv_id: str) -> dict[AgentId, TaskRecord]:
        return {t.agent_id: t for t in await runtime(self.app).repo.task_records(inv_id)}

    async def changes(
        self, inv_id: str, agent: AgentId
    ) -> list[tuple[int, str | None, str, str | None]]:
        """``(attempt, from_state, to_state, reason)`` of every transition of ``agent``'s task."""
        task_id = (await self.tasks(inv_id))[agent].id
        resp = await self.client.get(
            f"/api/v1/investigations/{inv_id}/timeline", params={"limit": 100}
        )
        events = [TimelineEvent.model_validate(e) for e in resp.json()["items"]]
        return [
            (
                e.payload["attempt"],
                e.payload["from_state"],
                e.payload["to_state"],
                e.payload.get("reason"),
            )
            for e in events
            if e.type == "task.status_changed" and e.payload["task_id"] == task_id
        ]


@pytest.fixture
def chaos_factory(make_settings, exporter, tmp_path: Path) -> Callable[..., Chaos]:
    """Builds deployments, each over its own DB file: ``chaos_factory(task_deadline_seconds=400)``
    overrides settings."""
    built = 0

    def build(**overrides: object) -> Chaos:
        nonlocal built
        built += 1
        overrides.setdefault("sqlite_path", tmp_path / f"chaos-{built}.sqlite")
        return Chaos(make_settings, exporter, **overrides)

    return build


@pytest.fixture
def chaos_app(chaos_factory: Callable[..., Chaos]) -> Chaos:
    """A deployment with default settings and fakes: both agents finish on their second poll.
    Tests turn the knobs on ``chaos_app.geo`` / ``.ctx`` before or while driving."""
    return chaos_factory()
