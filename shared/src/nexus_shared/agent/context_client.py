"""Client for ``POST {CONTEXT_SERVICE_URL}/v1/findings:batch`` (S2 FR-032, spec §8.5, §12.3).

One retry layer: up to ``retries`` extra attempts on connect error / timeout / 5xx, with the same
body (idempotent on ``task_id``). Outcomes:

- 201/200 → committed ids;
- 409 ``already_committed`` → the committed ids from ``AlreadyCommittedProblem`` (COMPLETED);
- 409 ``investigation_cancelled`` → FAILED(``investigation_cancelled``);
- retries exhausted → FAILED(``context_unavailable``);
- 422 or any other 4xx → FAILED(``submission_rejected``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.findings import (
    AlreadyCommittedProblem,
    FindingBatchRequest,
    FindingBatchResponse,
)
from nexus_contracts.api.problem import ProblemDetails

log = logging.getLogger(__name__)

BATCH_PATH = "/v1/findings:batch"


@dataclass(frozen=True)
class SubmitResult:
    finding_ids: list[str]
    error: ErrorCode | None = None
    already_committed: bool = False


class ContextServiceClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        retries: int = 2,
        backoff_s: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )
        self.retries = retries
        self.backoff_s = backoff_s

    @property
    def http(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def submit_batch(self, batch: FindingBatchRequest) -> SubmitResult:
        body = batch.model_dump(mode="json")
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(self.backoff_s * attempt)
            try:
                resp = await self._client.post(BATCH_PATH, json=body)
            except httpx.TransportError as exc:
                log.warning(
                    "context service unreachable",
                    extra={"error_type": type(exc).__name__, "try": attempt + 1},
                )
                continue
            if resp.status_code >= 500:
                log.warning("context service 5xx", extra={"status": resp.status_code})
                continue
            return self._interpret(resp)
        return SubmitResult([], ErrorCode.CONTEXT_UNAVAILABLE)

    @staticmethod
    def _interpret(resp: httpx.Response) -> SubmitResult:
        status = resp.status_code
        try:
            if status in (200, 201):
                return SubmitResult(FindingBatchResponse.model_validate(resp.json()).finding_ids)
            if status == 409:
                problem = ProblemDetails.model_validate(resp.json())
                if problem.code == ErrorCode.ALREADY_COMMITTED:
                    committed = AlreadyCommittedProblem.model_validate(resp.json())
                    return SubmitResult(committed.finding_ids, already_committed=True)
                if problem.code == ErrorCode.INVESTIGATION_CANCELLED:
                    return SubmitResult([], ErrorCode.INVESTIGATION_CANCELLED)
        except (ValueError, ValidationError):
            log.error("unparseable context service response", extra={"status": status})
        log.warning("batch rejected", extra={"status": status})
        return SubmitResult([], ErrorCode.SUBMISSION_REJECTED)
