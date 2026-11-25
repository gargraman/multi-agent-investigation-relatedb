import io
import json
import logging

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError

from nexus_shared.config import ServiceSettings
from nexus_shared.logging import JsonFormatter
from nexus_shared.security import TokenRegistry, resolve_actor, token_env_var
from nexus_shared.telemetry import build_tracer_provider, instrument_app, instrument_client
from nexus_shared.testing import InMemorySpanExporter

TOKEN = "t" * 32


def settings(name: str = "svc", **kw: object) -> ServiceSettings:
    return ServiceSettings(service_name=name, service_token=TOKEN, **kw)


def test_settings_fail_fast_on_short_token_and_bad_level() -> None:
    with pytest.raises(ValidationError):
        ServiceSettings(service_name="svc", service_token="short")
    with pytest.raises(ValidationError):
        settings(nexus_log_level="LOUD")


def test_settings_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_NAME", "context-service")
    monkeypatch.setenv("SERVICE_TOKEN", TOKEN)
    s = ServiceSettings()
    assert s.service_name == "context-service"
    assert s.otel_exporter_otlp_endpoint is None
    assert TOKEN not in repr(s)  # secret never printed


def test_json_log_carries_trace_ids() -> None:
    provider = build_tracer_provider(settings())
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("svc"))
    log = logging.getLogger("t.json")
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    with provider.get_tracer("t").start_as_current_span("op") as span:
        log.info("hello", extra={"investigation_id": "inv_1"})
        expected = format(span.get_span_context().trace_id, "032x")
    line = json.loads(stream.getvalue())
    assert line["trace_id"] == expected
    assert line["extra"] == {"investigation_id": "inv_1"}
    assert line["service"] == "svc"


async def test_trace_propagates_across_two_services() -> None:
    """Service A (client span) -> service B (FastAPI server span) share one trace id."""
    exporter = InMemorySpanExporter()
    provider_a = build_tracer_provider(settings("svc-a"), exporter)
    provider_b = build_tracer_provider(settings("svc-b"), exporter)

    app = FastAPI()
    instrument_app(app, provider_b)
    seen: dict[str, str] = {}

    @app.get("/work")
    async def work(request: Request) -> dict[str, str]:
        seen["traceparent"] = request.headers.get("traceparent", "")
        return {"ok": "yes"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ok"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://b"
    ) as client:
        instrument_client(client, provider_a)
        with provider_a.get_tracer("a").start_as_current_span("root") as root:
            assert (await client.get("/work")).status_code == 200
            await client.get("/readyz")
            trace_id = root.get_span_context().trace_id

    assert format(trace_id, "032x") in seen["traceparent"]
    spans = exporter.get_finished_spans()
    by_service = {
        s.resource.attributes["service.name"] for s in spans if s.context.trace_id == trace_id
    }
    assert by_service == {"svc-a", "svc-b"}
    # In-process ASGI marks the server span INTERNAL (a client span is already current), so
    # assert on names: the header proves injection, the shared trace id proves correlation.
    b_spans = [s.name for s in spans if s.resource.attributes["service.name"] == "svc-b"]
    assert "GET /work" in b_spans
    assert not any("readyz" in name for name in b_spans)  # probes excluded


def test_token_registry() -> None:
    env = {token_env_var("orchestrator"): "o" * 32, token_env_var("sigint-agent"): "s" * 40}
    reg = TokenRegistry.from_env(["orchestrator", "sigint-agent"], env)
    assert token_env_var("sigint-agent") == "NEXUS_TOKEN_SIGINT_AGENT"
    assert reg.authenticate("Bearer " + "o" * 32) == "orchestrator"
    assert reg.authenticate("Bearer " + "s" * 40) == "sigint-agent"
    assert reg.authenticate("Bearer wrong") is None
    assert reg.authenticate("o" * 32) is None
    assert reg.authenticate(None) is None
    with pytest.raises(ValueError, match="NEXUS_TOKEN_GEOINT_AGENT"):
        TokenRegistry.from_env(["geoint-agent"], env)


def test_actor_header_trusted_only_from_orchestrator() -> None:
    assert resolve_actor("orchestrator", "operator:demo") == "operator:demo"
    assert resolve_actor("sigint-agent", "operator:demo") == "sigint-agent"
    assert resolve_actor("orchestrator", None) == "orchestrator"
    assert resolve_actor("orchestrator", "operator:Robert'); DROP") == "orchestrator"
