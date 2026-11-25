"""OpenTelemetry setup (spec §15.2, §15.3).

- W3C ``traceparent``/``tracestate`` propagation (the OTel default) on every outbound httpx call
  and every inbound FastAPI request, so orchestrator → agent → Context Service is one trace.
- OTLP export only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (the ``lite`` profile has no LGTM).
- Tests pass an ``exporter`` (e.g. ``InMemorySpanExporter``) to assert on spans directly.
- Health and liveness probes are excluded from server spans, so polls don't flood traces.

Attribute allowlisting at the source (§15.6) is each call site's job: put identifiers on spans,
never content. The Collector redaction layer arrives with the LGTM overlay (S6).
"""

from typing import TYPE_CHECKING

import httpx
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from nexus_shared.config import ServiceSettings

if TYPE_CHECKING:
    from fastapi import FastAPI

EXCLUDED_URLS = "livez,readyz,healthz"


def build_tracer_provider(
    settings: ServiceSettings, exporter: SpanExporter | None = None
) -> TracerProvider:
    """Create a provider with Nexus resource attributes. Does not install it globally."""
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.nexus_env.value,
            "nexus.node.id": settings.service_name,
        }
    )
    provider = TracerProvider(resource=resource)
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        otlp = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(otlp))
    return provider


def setup_telemetry(
    settings: ServiceSettings, exporter: SpanExporter | None = None
) -> TracerProvider:
    """Install the provider globally (once per process) and return it."""
    provider = build_tracer_provider(settings, exporter)
    trace.set_tracer_provider(provider)
    return provider


def instrument_app(app: "FastAPI", provider: TracerProvider | None = None) -> None:
    """Server spans for every route except health probes; extracts inbound ``traceparent``."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls=EXCLUDED_URLS)


def instrument_client(client: httpx.AsyncClient, provider: TracerProvider | None = None) -> None:
    """Client spans + ``traceparent`` injection on one client (not process-wide)."""
    HTTPXClientInstrumentor.instrument_client(client, tracer_provider=provider)
