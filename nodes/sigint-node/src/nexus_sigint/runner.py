"""Background execution of accepted tasks (spec §8.3, ADR-003).

Each accepted task runs as an asyncio task inside the single worker:
SUBMITTED → WORKING → pipeline (pure) → one ``findings:batch`` submit → COMPLETED/FAILED.

- The agent self-cancels at ``deadline + grace`` (ADR-003 B3) while detecting/calling the LLM,
  never during the commit: a commit that started is not interrupted (§8.3).
- An unexpected internal error ends the attempt FAILED(``agent_restarted``): the same outcome a
  crash produces after the boot sweep, and re-dispatchable by the orchestrator (S3).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from nexus_contracts.a2a.task import AgentTaskStatus, TaskRequest, TaskUsage
from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCode
from nexus_shared.agent.catalog import ManifestCatalog
from nexus_shared.agent.context_client import ContextServiceClient
from nexus_shared.agent.store import TaskRow, TaskStore, utcnow
from nexus_shared.llm import LLMProvider, TokenBudget
from nexus_sigint import pipeline

log = logging.getLogger(__name__)


class TaskRunner:
    def __init__(
        self,
        store: TaskStore,
        provider: LLMProvider,
        catalog: ManifestCatalog,
        fixtures_root: Path,
        context: ContextServiceClient,
        *,
        deadline_grace_s: float = 5.0,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self.tracer = trace.get_tracer("nexus_sigint", tracer_provider=tracer_provider)
        self.store = store
        self.provider = provider
        self.catalog = catalog
        self.fixtures_root = fixtures_root
        self.context = context
        self.deadline_grace_s = deadline_grace_s
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, request: TaskRequest) -> asyncio.Task[None]:
        task = asyncio.create_task(self._guarded(request), name=f"task:{request.task_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Wait for running tasks (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        """Shutdown: cancel in-flight runs. Their rows stay WORKING; the next boot sweeps them."""
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _guarded(self, request: TaskRequest) -> None:
        ctx = TraceContextTextMapPropagator().extract(
            {
                "traceparent": request.trace_context.traceparent,
                "tracestate": request.trace_context.tracestate,
            }
        )
        with self.tracer.start_as_current_span("task.run", context=ctx) as span:
            span.set_attribute("nexus.task_id", request.task_id)
            span.set_attribute("nexus.attempt", request.attempt)
            try:
                await self._execute(request)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("task crashed", extra=_ids(request))
                await self._fail_latest(request, ErrorCode.AGENT_RESTARTED)

    async def _execute(self, request: TaskRequest) -> None:
        row = await self.store.get(request.task_id, request.attempt)
        if row is None or row.status is not AgentTaskStatus.SUBMITTED:
            return
        row = await self.store.update(
            request.task_id, request.attempt, row.version, status=AgentTaskStatus.WORKING.value
        )
        if row is None:
            return
        log.info("task working", extra=_ids(request))

        manifest = self.catalog.get(request.scenario_id)
        if manifest is None:  # validated at submit; only if the file vanished meanwhile
            await self._finish(row, AgentTaskStatus.FAILED, error=ErrorCode.TASK_REJECTED)
            return

        remaining = (request.deadline - datetime.now(UTC)).total_seconds() + self.deadline_grace_s
        try:
            if remaining <= 0:
                raise TimeoutError
            outcome = await asyncio.wait_for(
                pipeline.run(
                    request,
                    provider=self.provider,
                    manifest=manifest,
                    fixtures_root=self.fixtures_root,
                    budget=TokenBudget(request.token_budget),
                    tracer=self.tracer,
                ),
                timeout=remaining,
            )
        except TimeoutError:
            await self._finish(
                row,
                AgentTaskStatus.FAILED,
                error=ErrorCode.DEADLINE_EXCEEDED,
                summary="Task failed: deadline_exceeded.",
            )
            return

        common = {
            "usage": outcome.usage,
            "model_id": outcome.model_id,
            "prompt_version": outcome.prompt_version,
            "warnings": outcome.warnings,
        }
        if outcome.status is not AgentTaskStatus.COMPLETED:
            code = outcome.error.code if outcome.error else ErrorCode.AGENT_RESTARTED
            await self._finish(row, outcome.status, error=code, summary=outcome.summary, **common)
            return
        if outcome.batch is None:  # zero candidates: COMPLETED, no submit (FR-031)
            await self._finish(row, AgentTaskStatus.COMPLETED, summary=outcome.summary, **common)
            return

        result = await self.context.submit_batch(outcome.batch)
        if result.error is not None:
            await self._finish(
                row,
                AgentTaskStatus.FAILED,
                error=result.error,
                summary=f"Task failed: {result.error.value}.",
                **common,
            )
            return
        summary = outcome.summary
        if result.already_committed:
            summary += " An earlier attempt's batch was already committed; reporting its ids."
        await self._finish(
            row,
            AgentTaskStatus.COMPLETED,
            finding_ids=result.finding_ids,
            summary=summary,
            **common,
        )

    async def _finish(
        self,
        row: TaskRow,
        status: AgentTaskStatus,
        *,
        error: ErrorCode | None = None,
        summary: str | None = None,
        finding_ids: list[str] | None = None,
        usage: TaskUsage | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        usage = usage or TaskUsage(input_tokens=0, output_tokens=0, llm_calls=0)
        updated = await self.store.update(
            row["task_id"],
            row["attempt"],
            row.version,
            status=status.value,
            error_code=error.value if error else None,
            error_category=CATEGORY_BY_CODE[error].value if error else None,
            summary=summary,
            finding_ids_json=json.dumps(finding_ids or []),
            warnings_json=json.dumps(warnings or []),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            llm_calls=usage.llm_calls,
            usage_replayed=int(usage.replayed),
            model_id=model_id,
            prompt_version=prompt_version,
            completed_at=utcnow(),
        )
        if updated is None:
            log.warning(
                "terminal update dropped (version moved)", extra={"task_id": row["task_id"]}
            )
            return
        log.info(
            "task finished",
            extra={
                "task_id": row["task_id"],
                "attempt": row["attempt"],
                "investigation_id": row["investigation_id"],
                "status": status.value,
                "error_code": error.value if error else None,
                "llm_calls": usage.llm_calls,
            },
        )

    async def _fail_latest(self, request: TaskRequest, code: ErrorCode) -> None:
        row = await self.store.get(request.task_id, request.attempt)
        if row is not None and not row.status.is_terminal:
            await self._finish(row, AgentTaskStatus.FAILED, error=code, summary="Task failed.")


def _ids(request: TaskRequest) -> dict[str, object]:
    return {
        "task_id": request.task_id,
        "attempt": request.attempt,
        "investigation_id": request.investigation_id,
    }
