"""Orchestrator app (spec §6.2; ADR-005 D1-D5; ADR-008 F3).

Run with exactly one worker::

    uvicorn nexus_orchestrator.main:app --workers 1 --port 8000

Lifespan order (ADR-008 F3): open DB + schema → leader lock (wait ≤ 30 s, else startup fails) →
first agent-card read (E4 lock, FR-008) → resume sweep (re-register CREATED, restart drivers
for RUNNING) → background retrier + card refresh + close reconciler → serve. ``/readyz`` turns
green only after the sweep.

Shutdown (ADR-005 D5): mark not-ready, close SSE streams, cancel background tasks and drivers,
release the leader row, close clients and DB. No investigation or task status is changed.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider

from nexus_orchestrator import __version__
from nexus_orchestrator.api.problems import install_problem_handlers
from nexus_orchestrator.api.routes import api, probes
from nexus_orchestrator.api.runtime import Runtime
from nexus_orchestrator.clock import Clock, SystemClock
from nexus_orchestrator.delegation.agent_client import AgentClient
from nexus_orchestrator.delegation.agent_registry import AgentRegistry
from nexus_orchestrator.delegation.context_client import ContextServiceClient
from nexus_orchestrator.pubsub import TimelineBroker
from nexus_orchestrator.scenarios import ScenarioCatalog
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.leader import LeaderLock, LeaderTimings
from nexus_orchestrator.state.repository import Repository
from nexus_orchestrator.workflow.creation import InvestigationWorkflow
from nexus_orchestrator.workflow.dispatch import Dispatcher
from nexus_orchestrator.workflow.fence import Fence
from nexus_orchestrator.workflow.poller import Poller
from nexus_orchestrator.workflow.reconciler import CloseReconciler
from nexus_shared.telemetry import instrument_app, setup_telemetry

log = logging.getLogger(__name__)


def create_app(
    settings: OrchestratorSettings | None = None,
    context_transport: httpx.AsyncBaseTransport | None = None,
    *,
    agent_transport: httpx.AsyncBaseTransport | None = None,
    tracer_provider: TracerProvider | None = None,
    leader_holder: str | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the app. Tests inject ``context_transport`` / ``agent_transport`` (test doubles at
    the HTTP boundary) and a ``tracer_provider`` with an in-memory exporter."""
    settings = settings or OrchestratorSettings()  # type: ignore[call-arg]
    clock = clock or SystemClock()
    provider = tracer_provider or setup_telemetry(settings)
    tracer = provider.get_tracer("nexus_orchestrator", __version__)

    db = Database(settings.sqlite_path)
    broker = TimelineBroker()
    repo = Repository(db, broker)
    catalog = ScenarioCatalog(settings.scenarios_dir)
    context = ContextServiceClient(
        settings.context_service_url,
        settings.service_token.get_secret_value(),
        timeout=settings.context_service_timeout_seconds,
        probe_timeout=settings.health_probe_timeout_seconds,
        transport=context_transport,
        tracer_provider=provider,
    )
    leader = LeaderLock(
        db,
        LeaderTimings(
            heartbeat_seconds=settings.leader_heartbeat_seconds,
            stale_seconds=settings.leader_stale_seconds,
            wait_seconds=settings.leader_wait_seconds,
            poll_seconds=settings.leader_poll_seconds,
        ),
        holder=leader_holder,
    )
    agents = AgentClient(
        {a: settings.agent_url(a) for a in settings.nexus_agent_urls},
        settings.service_token.get_secret_value(),
        submit_timeout=settings.dispatch_timeout_seconds,
        poll_connect_timeout=settings.poll_connect_timeout_seconds,
        poll_read_timeout=settings.poll_read_timeout_seconds,
        probe_timeout=settings.health_probe_timeout_seconds,
        transport=agent_transport,
        tracer_provider=provider,
    )
    registry = AgentRegistry(
        agents, settings.nexus_enabled_agents, settings.agent_card_refresh_seconds
    )

    def is_leader() -> bool:
        return leader.held

    dispatcher = Dispatcher(settings, db, repo, agents, tracer, is_leader, clock)
    fence = Fence(settings, db, repo, context, agents, tracer, clock)
    poller = Poller(
        settings, db, repo, catalog, agents, dispatcher, fence, tracer, is_leader, clock
    )
    workflow = InvestigationWorkflow(
        settings, db, repo, catalog, context, tracer, poller, registry, clock
    )
    reconciler = CloseReconciler(settings, repo, db, context, tracer, clock, is_leader)
    runtime = Runtime(
        settings,
        db,
        broker,
        repo,
        catalog,
        context,
        leader,
        workflow,
        agents,
        registry,
        poller,
        fence,
        tracer,
        reconciler,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            await db.open()
            await leader.acquire()  # raises LeaderLockError after wait_seconds
            await registry.refresh()  # before the sweep, so E4 applies from the start
            await workflow.resume_sweep()
            workflow.start()
            registry.start()
            reconciler.start()
            runtime.sweep_done = True
            log.info("orchestrator ready", extra={"holder": leader.holder})
        except BaseException:
            await _shutdown(runtime)
            raise
        try:
            yield
        finally:
            await _shutdown(runtime)

    app = FastAPI(
        title="Nexus Orchestrator",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    install_problem_handlers(app)
    app.include_router(api)
    app.include_router(probes)
    instrument_app(app, provider)
    return app


async def _shutdown(rt: Runtime) -> None:
    rt.shutting_down = True
    rt.broker.close()
    await rt.workflow.stop()
    await rt.registry.stop()
    await rt.reconciler.stop()
    await rt.poller.stop()
    try:
        await rt.leader.release()
    except Exception:
        log.exception("failed to release leader lock")
    await rt.context.aclose()
    await rt.agents.aclose()
    await rt.db.close()
    log.info("orchestrator stopped")


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Module-level ``app`` built lazily (PEP 562), so importing this module in tests does not
    require production env vars. ``uvicorn nexus_orchestrator.main:app`` triggers it."""
    global _app
    if name == "app":
        if _app is None:
            from nexus_shared.logging import configure_logging

            settings = OrchestratorSettings()  # type: ignore[call-arg]
            configure_logging(settings.service_name, settings.nexus_log_level)
            _app = create_app(settings)
        return _app
    raise AttributeError(name)
