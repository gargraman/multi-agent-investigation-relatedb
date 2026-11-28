"""App factory and lifespan.

Run with exactly one worker (spec §4.3: one replica, one process owns the store)::

    uvicorn nexus_context_service.main:app --workers 1 --port 8000

Lifespan: open the lane -> bootstrap the schema -> ready. On shutdown (uvicorn turns SIGTERM into
lifespan shutdown, spec §16.2): not ready -> queued/in-flight transactions finish -> flush ->
close the store.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from nexus_context_service import __version__
from nexus_context_service.errors import install_error_handlers
from nexus_context_service.lane import RelateLane
from nexus_context_service.policy import Policy
from nexus_context_service.repository import GraphRepository
from nexus_context_service.routes import router
from nexus_context_service.settings import (
    ContextServiceSettings,
    assert_durability_full,
    load_token_registry,
)
from nexus_shared.logging import configure_logging
from nexus_shared.security import TokenRegistry
from nexus_shared.telemetry import build_tracer_provider, instrument_app, setup_telemetry

log = logging.getLogger(__name__)


def create_app(
    settings: ContextServiceSettings | None = None,
    *,
    token_registry: TokenRegistry | None = None,
    span_exporter: SpanExporter | None = None,
) -> FastAPI:
    """Build the app. Config errors (store path, tokens, durability mode, policy) raise here.

    ``token_registry`` / ``span_exporter`` are test seams; production loads tokens from the
    environment and exports spans per ``OTEL_EXPORTER_OTLP_ENDPOINT``."""
    settings = settings or ContextServiceSettings()  # type: ignore[call-arg]
    assert_durability_full()
    registry = token_registry or load_token_registry()
    policy = Policy.load(settings.policy_path)  # a malformed policy stops startup

    if span_exporter is not None:  # tests: a private provider, not the process-global one
        provider = build_tracer_provider(settings, span_exporter)
    else:
        provider = setup_telemetry(settings)

    lane = RelateLane(
        settings.relate_store_path,
        flush_interval_s=settings.relate_flush_interval_s,
        tracer_provider=provider,
    )
    repository = GraphRepository(lane)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await lane.start()
        try:
            await repository.bootstrap_schema()
            app.state.ready = True
            log.info("context service ready")
            yield
        finally:
            app.state.ready = False
            await lane.close()
            log.info("relate store closed")

    app = FastAPI(
        title="Nexus Context Service",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.token_registry = registry
    app.state.policy = policy
    app.state.lane = lane
    app.state.repository = repository
    app.state.ready = False
    install_error_handlers(app)
    app.include_router(router)
    instrument_app(app, provider)
    return app


def _default_app() -> FastAPI:
    settings = ContextServiceSettings()  # type: ignore[call-arg]
    configure_logging(settings.service_name, settings.nexus_log_level)
    return create_app(settings)


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Lazy module-level ``app`` (PEP 562), so importing this module (tests, tooling) doesn't
    require production env vars; ``uvicorn nexus_context_service.main:app`` still works."""
    global _app
    if name == "app":
        if _app is None:
            _app = _default_app()
        return _app
    raise AttributeError(name)
