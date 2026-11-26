"""Specialist-agent app factory and lifespan, driven by an :class:`AgentSpec`.

Run with exactly one worker (background tasks and the SQLite writer live in this process)::

    uvicorn nexus_<agent>.main:app --workers 1 --port 8000

Lifespan: open SQLite + boot sweep (FR-024) → ready. Shutdown: not ready → cancel in-flight runs
(rows stay WORKING; the next boot sweeps them) → close clients and DB.

Mode is fixed at startup by ``NEXUS_LLM_MODE`` (ADR-006 E6): ``live`` builds the OpenAI provider
(no OpenAI call at startup, config validation only), ``replay`` builds the replay provider.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from nexus_contracts.common import LlmMode
from nexus_shared.agent.api import install_error_handlers, router
from nexus_shared.agent.catalog import ManifestCatalog
from nexus_shared.agent.context_client import ContextServiceClient
from nexus_shared.agent.runner import TaskRunner
from nexus_shared.agent.settings import AgentSettings, load_token_registry
from nexus_shared.agent.spec import AgentSpec
from nexus_shared.agent.store import TaskStore
from nexus_shared.llm import LLMProvider, ReplayLLMProvider
from nexus_shared.logging import configure_logging
from nexus_shared.security import TokenRegistry
from nexus_shared.telemetry import (
    build_tracer_provider,
    instrument_app,
    instrument_client,
    setup_telemetry,
)

log = logging.getLogger(__name__)


def build_provider(settings: AgentSettings) -> LLMProvider:
    if settings.nexus_llm_mode is LlmMode.REPLAY:
        return ReplayLLMProvider(settings.replay_dir, latency_ms=settings.replay_latency_ms)
    from nexus_shared.llm import OpenAIProvider, build_ssl_context

    assert settings.openai_api_key is not None  # enforced by settings validation
    return OpenAIProvider(
        model=settings.openai_model_extract,
        api_key=settings.openai_api_key,
        timeout_s=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
        ssl_context=build_ssl_context(settings.extra_ca_file),
        reasoning_effort=settings.openai_reasoning_effort,
    )


def create_agent_app(
    spec: AgentSpec,
    settings: AgentSettings,
    *,
    token_registry: TokenRegistry | None = None,
    provider: LLMProvider | None = None,
    context_transport: httpx.AsyncBaseTransport | None = None,
    span_exporter: SpanExporter | None = None,
) -> FastAPI:
    """Build the app for ``spec``. ``token_registry``, ``provider``, ``context_transport`` (an
    ``httpx.MockTransport`` at the HTTP boundary) and ``span_exporter`` are test seams."""
    short = spec.agent_id.value.split("-")[0]  # "<name>-agent" -> "<name>"
    registry = token_registry or load_token_registry()
    if span_exporter is not None:
        tracer_provider = build_tracer_provider(settings, span_exporter)
    else:
        tracer_provider = setup_telemetry(settings)

    llm = provider or build_provider(settings)
    store = TaskStore(settings.sqlite_path, spec.agent_id)
    catalog = ManifestCatalog(settings.agent_manifests_dir)
    context = ContextServiceClient(
        settings.context_service_url,
        settings.service_token.get_secret_value(),
        timeout=settings.context_timeout_seconds,
        retries=settings.context_retries,
        backoff_s=settings.context_backoff_seconds,
        transport=context_transport,
    )
    instrument_client(context.http, tracer_provider)
    runner = TaskRunner(
        store,
        llm,
        catalog,
        settings.fixtures_dir,
        context,
        spec,
        deadline_grace_s=settings.deadline_grace_seconds,
        tracer_provider=tracer_provider,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        swept = await store.open()
        if swept:
            log.warning("boot sweep marked tasks agent_restarted", extra={"count": swept})
        app.state.ready = True
        log.info("%s agent ready", short, extra={"llm_mode": settings.nexus_llm_mode.value})
        try:
            yield
        finally:
            app.state.ready = False
            await runner.stop()
            await context.aclose()
            aclose = getattr(llm, "aclose", None)
            if aclose is not None:
                await aclose()
            await store.close()

    app = FastAPI(title=f"Nexus {short.upper()} agent", version=spec.version, lifespan=lifespan)
    app.state.spec = spec
    app.state.settings = settings
    app.state.token_registry = registry
    app.state.store = store
    app.state.catalog = catalog
    app.state.runner = runner
    app.state.provider = llm
    app.state.ready = False
    install_error_handlers(app)
    app.include_router(router)
    instrument_app(app, tracer_provider)
    return app


def lazy_app(spec: AgentSpec, settings_cls: type[AgentSettings]) -> Callable[[str], Any]:
    """A PEP 562 module ``__getattr__`` exposing a lazily built ``app``, so importing an agent's
    ``main`` module needs no env vars while ``uvicorn nexus_<agent>.main:app`` still works::

        __getattr__ = lazy_app(MY_SPEC, MyAgentSettings)
    """
    built: list[FastAPI] = []

    def __getattr__(name: str) -> Any:
        if name == "app":
            if not built:
                settings = settings_cls()  # type: ignore[call-arg]
                configure_logging(settings.service_name, settings.nexus_log_level)
                built.append(create_agent_app(spec, settings))
            return built[0]
        raise AttributeError(name)

    return __getattr__
