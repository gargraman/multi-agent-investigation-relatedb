"""Agent task API client (main spec §12.2; ADR-003, ADR-004; S2 spec FR-003, FR-004, FR-008).

Three httpx clients, one per purpose:

- **submit** (``POST /v1/tasks``): OTel-instrumented, so ``traceparent`` goes in the header; the
  caller also puts it in ``TaskRequest.trace_context``. Bearer = the orchestrator's token.
- **poll** (``GET /v1/tasks/{id}``): **uninstrumented** (no span per poll, ADR-008 F9), 2 s
  connect / 5 s read (§8.3).
- **probe** (``GET /.well-known/agent-card.json``): uninstrumented, unauthenticated (S2 G20).

Outcomes are returned as values, never raised: the workflow decides what to do with them. This
module is the transport adapter that the official A2A SDK could replace later (§12.2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

import httpx
from opentelemetry.sdk.trace import TracerProvider
from pydantic import ValidationError

from nexus_contracts.a2a.status_mapping import PollFailure
from nexus_contracts.a2a.task import (
    AgentCard,
    AgentTaskStatus,
    TaskRequest,
    TaskResult,
    TaskSubmitResponse,
)
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.common import AgentId
from nexus_shared.telemetry import instrument_client

log = logging.getLogger(__name__)


class SubmitKind(StrEnum):
    ACKED = "acked"  # 202 new task or 200 duplicate (status = the agent's current status)
    UNAVAILABLE = "unavailable"  # connect error, timeout, 5xx, 408/429: retry, task stays PENDING
    REFUSED = "refused"  # any other 4xx (409 content_conflict/stale_attempt, 401, 422 …)


@dataclass(frozen=True)
class SubmitOutcome:
    kind: SubmitKind
    detail: str
    status: AgentTaskStatus | None = None  # ACKED only
    duplicate: bool = False  # ACKED via 200
    code: ErrorCode | None = None  # REFUSED: the problem code, if it is a known one

    @property
    def retryable(self) -> bool:
        return self.kind is SubmitKind.UNAVAILABLE


@dataclass(frozen=True)
class CardOutcome:
    card: AgentCard | None
    detail: str


class _Delegating(httpx.AsyncBaseTransport):
    """A per-client wrapper around a shared (injected) transport."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._inner.handle_async_request(request)


def _own(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncBaseTransport | None:
    """``None`` → httpx builds a fresh pool per client (production); else a private wrapper."""
    return None if transport is None else _Delegating(transport)


class AgentClient:
    def __init__(
        self,
        urls: dict[AgentId, str],
        token: str,
        *,
        submit_timeout: float,
        poll_connect_timeout: float,
        poll_read_timeout: float,
        probe_timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._probe_timeout = probe_timeout
        self._urls = {agent: url.rstrip("/") for agent, url in urls.items()}
        auth = {"Authorization": f"Bearer {token}"}
        # OTel's instrument_client patches the client's *transport instance*. Each client gets its
        # own transport object, so instrumenting submit can never trace polls or probes (F9), even
        # when tests inject one shared transport.
        self._submit = httpx.AsyncClient(
            timeout=submit_timeout, transport=_own(transport), headers=auth
        )
        instrument_client(self._submit, tracer_provider)
        self._poll = httpx.AsyncClient(
            timeout=httpx.Timeout(poll_read_timeout, connect=poll_connect_timeout),
            transport=_own(transport),
            headers=auth,
        )
        self._probe = httpx.AsyncClient(timeout=probe_timeout, transport=_own(transport))

    async def aclose(self) -> None:
        for client in (self._submit, self._poll, self._probe):
            await client.aclose()

    def _url(self, agent: AgentId, path: str) -> str:
        return f"{self._urls[agent]}{path}"

    async def submit(self, agent: AgentId, request: TaskRequest) -> SubmitOutcome:
        try:
            resp = await self._submit.post(
                self._url(agent, "/v1/tasks"), json=request.model_dump(mode="json")
            )
        except httpx.HTTPError as exc:
            return SubmitOutcome(SubmitKind.UNAVAILABLE, type(exc).__name__)
        if resp.status_code in (200, 202):
            try:
                body = TaskSubmitResponse.model_validate_json(resp.content)
            except ValidationError:
                # The agent may well have stored the task; resubmitting the same attempt is safe.
                return SubmitOutcome(SubmitKind.UNAVAILABLE, "malformed TaskSubmitResponse")
            if body.task_id != request.task_id:
                return SubmitOutcome(SubmitKind.UNAVAILABLE, "TaskSubmitResponse for another task")
            return SubmitOutcome(
                SubmitKind.ACKED,
                f"HTTP {resp.status_code}",
                status=body.status,
                duplicate=resp.status_code == 200,
            )
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            return SubmitOutcome(SubmitKind.UNAVAILABLE, f"HTTP {resp.status_code}")
        return SubmitOutcome(
            SubmitKind.REFUSED, f"HTTP {resp.status_code}", code=_problem_code(resp)
        )

    async def get_task(
        self, agent: AgentId, task_id: str, attempt: int
    ) -> TaskResult | PollFailure:
        """One poll. Anything that isn't a usable ``TaskResult`` for this attempt or a 404 is
        TRANSIENT: keep polling (§8.3, ADR-003 B7)."""
        try:
            resp = await self._poll.get(self._url(agent, f"/v1/tasks/{task_id}"))
        except httpx.HTTPError:
            return PollFailure.TRANSIENT
        if resp.status_code == 404:
            return PollFailure.NOT_FOUND
        if resp.status_code != 200:
            return PollFailure.TRANSIENT
        try:
            result = TaskResult.model_validate_json(resp.content)
        except ValidationError:
            log.warning("agent returned an invalid TaskResult", extra={"task_id": task_id})
            return PollFailure.TRANSIENT
        if result.task_id != task_id or result.attempt != attempt:
            log.warning(
                "poll result is for another task/attempt; ignoring",
                extra={"task_id": task_id, "attempt": attempt, "got_attempt": result.attempt},
            )
            return PollFailure.TRANSIENT
        return result

    async def card(self, agent: AgentId) -> CardOutcome:
        try:
            resp = await self._probe.get(self._url(agent, "/.well-known/agent-card.json"))
        except httpx.HTTPError as exc:
            return CardOutcome(None, type(exc).__name__)
        if resp.status_code != 200:
            return CardOutcome(None, f"HTTP {resp.status_code}")
        try:
            card = AgentCard.model_validate_json(resp.content)
        except ValidationError:
            return CardOutcome(None, "malformed agent card")
        if card.id != agent:
            return CardOutcome(None, f"card id {card.id} does not match {agent}")
        return CardOutcome(card, "ok")

    async def ready(self, agent: AgentId) -> bool:
        """``GET /readyz`` (unauthenticated, uninstrumented like ``card``). True only on 200;
        never raises: an unknown agent, connect error, timeout or 5xx is simply not ready."""
        if agent not in self._urls:
            return False
        try:
            resp = await self._probe.get(self._url(agent, "/readyz"))
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def cancel(self, agent: AgentId, task_id: str) -> bool:
        """``POST /v1/tasks/{id}/cancel`` with the orchestrator token, one ``probe_timeout``
        bound per call (the caller bounds the total). True on 200, or on 404 (the agent no longer
        knows the task, which is the goal anyway). Never raises. Rides the instrumented submit
        client so each cancel is its own span and carries ``traceparent``."""
        if agent not in self._urls:
            return False
        try:
            resp = await self._submit.post(
                self._url(agent, f"/v1/tasks/{task_id}/cancel"), timeout=self._probe_timeout
            )
        except httpx.HTTPError:
            return False
        return resp.status_code in (200, 404)


def _problem_code(resp: httpx.Response) -> ErrorCode | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    code = data.get("code") if isinstance(data, dict) else None
    try:
        return ErrorCode(code) if isinstance(code, str) else None
    except ValueError:
        return None
