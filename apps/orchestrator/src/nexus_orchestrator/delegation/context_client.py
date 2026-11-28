"""Context Service HTTP client (spec §12.3). The orchestrator never imports the service itself.

- ``create_investigation`` posts ``ContextInvestigationCreate`` (built with ``from_manifest``)
  with the orchestrator's bearer token. The client is OTel-instrumented, so ``traceparent``
  reaches the Context Service (spec §15.3).
- Health probes use a second, **uninstrumented** client so polling does not flood traces
  (ADR-008 F9).
- Outcomes are returned as values, not raised: callers decide whether to retry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
from opentelemetry.sdk.trace import TracerProvider
from pydantic import TypeAdapter, ValidationError

from nexus_contracts.api.context import (
    ContextInvestigationClose,
    ContextInvestigationCreate,
    ContextInvestigationCreated,
    InvestigationContext,
)
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.graph import ContradictionPair, Graph, HiddenLink, Provenance
from nexus_contracts.api.retraction import RetractEvidenceRequest, RetractionImpact
from nexus_contracts.api.states import InvestigationStatus
from nexus_shared.security import ACTOR_HEADER
from nexus_shared.telemetry import instrument_client

log = logging.getLogger(__name__)


class RegistrationOutcome(StrEnum):
    REGISTERED = "registered"
    UNAVAILABLE = "unavailable"  # transient: connect error, timeout, 5xx -> retry
    REJECTED = "rejected"  # permanent: 409 content_conflict / investigation_cancelled, other 4xx


@dataclass(frozen=True)
class RegistrationResult:
    outcome: RegistrationOutcome
    detail: str
    created: bool | None = None  # REGISTERED only: False when an identical create was replayed
    code: str | None = None  # ProblemDetails.code on REJECTED

    @property
    def retryable(self) -> bool:
        return self.outcome is RegistrationOutcome.UNAVAILABLE


@dataclass(frozen=True)
class ContextRead:
    context: InvestigationContext | None
    status: int  # 200, 404 (unknown there), 502 (bad response), 503 (unreachable / 5xx)
    detail: str


@dataclass(frozen=True)
class ProxyRead[T]:
    """Outcome of one proxied Context Service call (S4a FR-220); returned, not raised.

    ``value`` is set on a 2xx with a valid body, and ``status`` is then the Context Service's own
    code (retract needs 201 vs 200). Otherwise ``unavailable`` means unreachable, 5xx or a body
    that does not match the contract (the proxy answers 502), and ``status`` / ``detail`` /
    ``code`` carry a 4xx for the caller to pass through."""

    value: T | None
    status: int
    detail: str
    code: str | None = None
    unavailable: bool = False


_GRAPH = TypeAdapter(Graph)
_PROVENANCE = TypeAdapter(Provenance)
_CONTRADICTIONS = TypeAdapter(list[ContradictionPair])
_LINKS = TypeAdapter(list[HiddenLink])
_IMPACT = TypeAdapter(RetractionImpact)


class ContextServiceClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float,
        probe_timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )
        instrument_client(self._client, tracer_provider)
        self._probe = httpx.AsyncClient(
            base_url=base_url, timeout=probe_timeout, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._probe.aclose()

    async def create_investigation(self, body: ContextInvestigationCreate) -> RegistrationResult:
        try:
            resp = await self._client.post("/v1/investigations", json=body.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            return RegistrationResult(RegistrationOutcome.UNAVAILABLE, type(exc).__name__)
        if resp.status_code == 200 or resp.status_code == 201:
            try:
                created = ContextInvestigationCreated.model_validate_json(resp.content)
            except ValidationError:
                return RegistrationResult(
                    RegistrationOutcome.UNAVAILABLE, "malformed Context Service response"
                )
            return RegistrationResult(RegistrationOutcome.REGISTERED, "ok", created=created.created)
        code = _problem_code(resp)
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            return RegistrationResult(
                RegistrationOutcome.UNAVAILABLE, f"HTTP {resp.status_code}", code=code
            )
        return RegistrationResult(
            RegistrationOutcome.REJECTED, f"HTTP {resp.status_code}", code=code
        )

    async def get_context(self, investigation_id: str) -> ContextRead:
        """``GET /v1/investigations/{id}/context`` (S2 FR-044), for the findings proxy (FR-009)."""
        try:
            resp = await self._client.get(f"/v1/investigations/{investigation_id}/context")
        except httpx.HTTPError as exc:
            return ContextRead(None, 503, type(exc).__name__)
        if resp.status_code == 200:
            try:
                return ContextRead(
                    InvestigationContext.model_validate_json(resp.content), 200, "ok"
                )
            except ValidationError:
                log.error(
                    "Context Service returned an invalid InvestigationContext",
                    extra={"investigation_id": investigation_id},
                )
                return ContextRead(None, 502, "malformed Context Service response")
        if resp.status_code == 404:
            return ContextRead(None, 404, "not registered with the Context Service")
        return ContextRead(
            None, 503 if resp.status_code >= 500 else 502, f"HTTP {resp.status_code}"
        )

    async def get_graph(self, investigation_id: str) -> ProxyRead[Graph]:
        """``GET /v1/investigations/{id}/graph`` (I6)."""
        return await self._proxy("GET", f"/v1/investigations/{investigation_id}/graph", _GRAPH)

    async def get_provenance(self, assessment_id: str) -> ProxyRead[Provenance]:
        """``GET /v1/assessments/{id}/provenance`` (I1)."""
        return await self._proxy("GET", f"/v1/assessments/{assessment_id}/provenance", _PROVENANCE)

    async def get_contradictions(
        self, investigation_id: str, entity_id: str
    ) -> ProxyRead[list[ContradictionPair]]:
        """``GET /v1/investigations/{id}/entities/{entity_id}/contradictions`` (I3)."""
        path = f"/v1/investigations/{investigation_id}/entities/{entity_id}/contradictions"
        return await self._proxy("GET", path, _CONTRADICTIONS)

    async def get_links(self, investigation_id: str) -> ProxyRead[list[HiddenLink]]:
        """``GET /v1/investigations/{id}/links`` (I4)."""
        return await self._proxy("GET", f"/v1/investigations/{investigation_id}/links", _LINKS)

    async def retract_evidence(
        self,
        investigation_id: str,
        evidence_id: str,
        actor: str,
        body: RetractEvidenceRequest | None,
    ) -> ProxyRead[RetractionImpact]:
        """``POST /v1/investigations/{id}/evidence/{evidence_id}/retract`` (I2) as ``actor``
        (``X-Nexus-Actor``). ``status`` is 201 when newly retracted, 200 when it already was."""
        path = f"/v1/investigations/{investigation_id}/evidence/{evidence_id}/retract"
        return await self._proxy(
            "POST",
            path,
            _IMPACT,
            headers={ACTOR_HEADER: actor},
            json=body.model_dump(mode="json", exclude_none=True) if body else None,
        )

    async def _proxy[T](
        self,
        method: str,
        path: str,
        adapter: TypeAdapter[T],
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
    ) -> ProxyRead[T]:
        try:
            resp = await self._client.request(method, path, headers=headers, json=json)
        except httpx.HTTPError as exc:
            return ProxyRead(None, 503, type(exc).__name__, unavailable=True)
        if resp.is_success:
            try:
                return ProxyRead(adapter.validate_json(resp.content), resp.status_code, "ok")
            except ValidationError:
                log.error("Context Service returned an invalid body", extra={"path": path})
                return ProxyRead(None, 502, "malformed Context Service response", unavailable=True)
        if resp.status_code >= 500:
            return ProxyRead(None, resp.status_code, f"HTTP {resp.status_code}", unavailable=True)
        detail, code = _problem_fields(resp)
        return ProxyRead(None, resp.status_code, detail or f"HTTP {resp.status_code}", code)

    async def close(
        self, investigation_id: str, status: InvestigationStatus, *, timeout: float
    ) -> bool:
        """``POST /v1/investigations/{id}/close``: fence the investigation at the Context Service.

        True when it is closed or there is nothing left to fence: 200, 409
        ``investigation_cancelled`` (already closed), 404 (never registered). Anything else,
        including a transport failure, is False. Never raises; the caller retries or defers to
        the reconciler."""
        try:
            resp = await self._client.post(
                f"/v1/investigations/{investigation_id}/close",
                json=ContextInvestigationClose(status=status).model_dump(mode="json"),
                timeout=timeout,
            )
        except httpx.HTTPError:
            return False
        if resp.status_code in (200, 404):
            return True
        return (
            resp.status_code == 409
            and _problem_code(resp) == ErrorCode.INVESTIGATION_CANCELLED.value
        )

    async def readyz(self) -> tuple[bool, str]:
        try:
            resp = await self._probe.get("/readyz")
        except httpx.HTTPError as exc:
            return False, type(exc).__name__
        return resp.status_code == 200, f"HTTP {resp.status_code}"


def _problem_code(resp: httpx.Response) -> str | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    code = data.get("code") if isinstance(data, dict) else None
    return code if isinstance(code, str) else None


def _problem_fields(resp: httpx.Response) -> tuple[str | None, str | None]:
    """``(detail or title, code)`` of a ProblemDetails body; ``None`` where absent or not text."""
    try:
        data = resp.json()
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    detail = data.get("detail") or data.get("title")
    code = data.get("code")
    return (
        detail if isinstance(detail, str) else None,
        code if isinstance(code, str) else None,
    )
