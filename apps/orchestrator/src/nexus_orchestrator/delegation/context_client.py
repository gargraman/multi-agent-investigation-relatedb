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

import httpx
from opentelemetry.sdk.trace import TracerProvider
from pydantic import ValidationError

from nexus_contracts.api.context import (
    ContextInvestigationCreate,
    ContextInvestigationCreated,
    InvestigationContext,
)
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
