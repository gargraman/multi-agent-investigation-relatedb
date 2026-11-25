from __future__ import annotations

from collections.abc import Callable

import httpx

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.findings import FindingBatchRequest
from nexus_shared.agent.context_client import ContextServiceClient

from .conftest import make_batch


async def _batch() -> FindingBatchRequest:
    return make_batch()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> ContextServiceClient:
    return ContextServiceClient(
        "http://cs", "s" * 40, backoff_s=0, transport=httpx.MockTransport(handler)
    )


def _problem(status: int, **kw: object) -> httpx.Response:
    return httpx.Response(status, json={"title": "x", "status": status, **kw})


async def test_created() -> None:
    client = _client(
        lambda r: httpx.Response(
            201, json={"task_id": "tsk_test1", "finding_ids": ["fnd_a", "fnd_b"]}
        )
    )
    res = await client.submit_batch(await _batch())
    assert res.finding_ids == ["fnd_a", "fnd_b"] and res.error is None


async def test_already_committed_and_cancelled() -> None:
    client = _client(lambda r: _problem(409, code="already_committed", finding_ids=["fnd_x"]))
    res = await client.submit_batch(await _batch())
    assert res.already_committed and res.finding_ids == ["fnd_x"] and res.error is None
    client = _client(lambda r: _problem(409, code="investigation_cancelled"))
    assert (await client.submit_batch(await _batch())).error is ErrorCode.INVESTIGATION_CANCELLED


async def test_5xx_retried_then_unavailable() -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _problem(503)

    res = await _client(handler).submit_batch(await _batch())
    assert res.error is ErrorCode.CONTEXT_UNAVAILABLE and len(calls) == 3


async def test_connect_error_retried_with_same_body() -> None:
    bodies: list[bytes] = []

    def handler(r: httpx.Request) -> httpx.Response:
        bodies.append(r.content)
        if len(bodies) == 1:
            raise httpx.ConnectError("down", request=r)
        return httpx.Response(201, json={"task_id": "tsk_test1", "finding_ids": ["fnd_a"]})

    res = await _client(handler).submit_batch(await _batch())
    assert res.finding_ids == ["fnd_a"] and len(bodies) == 2 and bodies[0] == bodies[1]


async def test_4xx_is_submission_rejected_without_retry() -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _problem(422, code="schema_invalid")

    res = await _client(handler).submit_batch(await _batch())
    assert res.error is ErrorCode.SUBMISSION_REJECTED and len(calls) == 1
