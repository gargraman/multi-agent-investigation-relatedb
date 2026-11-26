"""S3b delegation-client additions: agent readiness/cancel and Context Service close."""

from __future__ import annotations

import httpx
import pytest

from nexus_contracts.api.context import ContextInvestigationClose
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus
from nexus_contracts.common import AgentId
from nexus_orchestrator.delegation.agent_client import AgentClient
from nexus_orchestrator.delegation.context_client import ContextServiceClient

AGENT_URL = "http://sigint.test"
INV = "inv-1"


def _agent(handler: httpx.MockTransport | None, *, urls: dict[AgentId, str] | None = None):
    return AgentClient(
        urls if urls is not None else {AgentId.SIGINT: AGENT_URL},
        "orch-token",
        submit_timeout=5,
        poll_connect_timeout=2,
        poll_read_timeout=5,
        probe_timeout=1,
        transport=handler,
    )


def _cs(handler: httpx.MockTransport) -> ContextServiceClient:
    return ContextServiceClient(
        "http://cs.test", "orch-token", timeout=5, probe_timeout=1, transport=handler
    )


async def test_ready_true_on_200() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    client = _agent(httpx.MockTransport(handler))
    assert await client.ready(AgentId.SIGINT) is True
    assert (seen[0].method, str(seen[0].url)) == ("GET", f"{AGENT_URL}/readyz")
    assert "authorization" not in seen[0].headers
    await client.aclose()


@pytest.mark.parametrize("failure", ["503", "timeout", "connect"])
async def test_ready_false_on_503_timeout_connect_error(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "connect":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(503)

    client = _agent(httpx.MockTransport(handler))
    assert await client.ready(AgentId.SIGINT) is False
    await client.aclose()


async def test_ready_false_for_unknown_agent() -> None:
    client = _agent(httpx.MockTransport(lambda r: httpx.Response(200)), urls={})
    assert await client.ready(AgentId.SIGINT) is False
    await client.aclose()


@pytest.mark.parametrize("status", [200, 404])
async def test_cancel_sends_token_and_true_on_200_404(status: int) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"task_id": "t-1"})

    client = _agent(httpx.MockTransport(handler))
    assert await client.cancel(AgentId.SIGINT, "t-1") is True
    assert (seen[0].method, str(seen[0].url)) == ("POST", f"{AGENT_URL}/v1/tasks/t-1/cancel")
    assert seen[0].headers["authorization"] == "Bearer orch-token"
    assert seen[0].content == b""
    await client.aclose()


@pytest.mark.parametrize("failure", ["connect", "timeout", "500", "401"])
async def test_cancel_false_on_connect_error(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError("refused", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(int(failure))

    client = _agent(httpx.MockTransport(handler))
    assert await client.cancel(AgentId.SIGINT, "t-1") is False
    await client.aclose()


async def test_cancel_false_for_unknown_agent() -> None:
    client = _agent(httpx.MockTransport(lambda r: httpx.Response(200)), urls={})
    assert await client.cancel(AgentId.SIGINT, "t-1") is False
    await client.aclose()


_CLOSED = {
    "investigation_id": INV,
    "status": "CANCELLED",
    "closed_at": "2026-01-01T00:00:00Z",
}


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("200", True),
        ("404", True),
        ("409-cancelled", True),
        ("409-other", False),
        ("500", False),
        ("timeout", False),
        ("connect", False),
    ],
)
async def test_close_body_and_outcomes(kind: str, expected: bool) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if kind == "connect":
            raise httpx.ConnectError("refused", request=request)
        if kind == "200":
            return httpx.Response(200, json=_CLOSED)
        if kind == "404":
            return httpx.Response(404, json={"code": "not_found"})
        if kind == "409-cancelled":
            return httpx.Response(409, json={"code": ErrorCode.INVESTIGATION_CANCELLED.value})
        if kind == "409-other":
            return httpx.Response(409, json={"code": ErrorCode.CONTENT_CONFLICT.value})
        return httpx.Response(500)

    client = _cs(httpx.MockTransport(handler))
    assert await client.close(INV, InvestigationStatus.CANCELLED, timeout=2.0) is expected
    request = seen[0]
    assert (request.method, str(request.url)) == (
        "POST",
        f"http://cs.test/v1/investigations/{INV}/close",
    )
    assert request.headers["authorization"] == "Bearer orch-token"
    assert (
        ContextInvestigationClose.model_validate_json(request.content).status
        is InvestigationStatus.CANCELLED
    )
    await client.aclose()


async def test_close_passes_per_call_timeout() -> None:
    seen: list[dict[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=_CLOSED)

    client = _cs(httpx.MockTransport(handler))
    assert await client.close(INV, InvestigationStatus.CANCELLED, timeout=2.5) is True
    assert seen[0]["read"] == 2.5
    await client.aclose()
