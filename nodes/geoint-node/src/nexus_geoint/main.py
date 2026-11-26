"""GEOINT agent app: the shared agent runtime with the :data:`~nexus_geoint.spec.GEOINT` spec.

Run with exactly one worker (background tasks and the SQLite writer live in this process)::

    uvicorn nexus_geoint.main:app --workers 1 --port 8000

``app`` is a lazy module attribute (PEP 562): importing this module needs no env vars. Lifespan,
provider selection and the HTTP API live in :mod:`nexus_shared.agent.app`.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SpanExporter

from nexus_geoint.settings import GeointSettings
from nexus_geoint.spec import GEOINT
from nexus_shared.agent.app import build_provider, create_agent_app, lazy_app
from nexus_shared.llm import LLMProvider
from nexus_shared.security import TokenRegistry

__all__ = ["build_provider", "create_app"]


def create_app(
    settings: GeointSettings | None = None,
    *,
    token_registry: TokenRegistry | None = None,
    provider: LLMProvider | None = None,
    context_transport: httpx.AsyncBaseTransport | None = None,
    span_exporter: SpanExporter | None = None,
) -> FastAPI:
    """Build the GEOINT app. The keyword arguments are the test seams of
    :func:`~nexus_shared.agent.app.create_agent_app`."""
    return create_agent_app(
        GEOINT,
        settings or GeointSettings(),  # type: ignore[call-arg]
        token_registry=token_registry,
        provider=provider,
        context_transport=context_transport,
        span_exporter=span_exporter,
    )


__getattr__ = lazy_app(GEOINT, GeointSettings)
