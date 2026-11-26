"""Background execution of accepted tasks (spec §8.3, ADR-003).

Each accepted task runs as an asyncio task inside the single worker:
SUBMITTED → WORKING → ``spec.run`` (pure pipeline) → one ``findings:batch`` submit →
COMPLETED/FAILED. Nothing here is agent-specific: the :class:`AgentSpec` supplies the pipeline.

- The agent self-cancels at ``deadline + grace`` (ADR-003 B3) while detecting/calling the LLM,
  never during the commit: a commit that started is not interrupted (§8.3).
- An unexpected internal error ends the attempt FAILED(``internal_error``). Only the boot sweep
  produces ``agent_restarted`` (S3).
- Agent-side cancel (S3 FR-102–106): cancel points are before ``spec.run`` and before the commit;
  a cancel that arrives while ``spec.run`` is in flight also cancels that asyncio task. A commit
  that started is never interrupted; a Context Service 409 ``investigation_cancelled`` ends the
  attempt CANCELED. Shutdown (:meth:`TaskRunner.stop`) is not a cancel: its rows stay WORKING for
  the next boot sweep. The two are told apart by :attr:`_Run.interrupted`, which only
  :meth:`TaskRunner.cancel` sets, and by ``_stopping``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from nexus_contracts.a2a.task import AgentTaskStatus, TaskRequest, TaskUsage
from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCode
from nexus_shared.agent.catalog import ManifestCatalog
from nexus_shared.agent.context_client import ContextServiceClient
from nexus_shared.agent.spec import AgentSpec
from nexus_shared.agent.store import TaskRow, TaskStore, utcnow
from nexus_shared.llm import LLMProvider, TokenBudget

log = logging.getLogger(__name__)


Phase = Literal["pending", "run", "commit"]  # "run": in spec.run; "commit": in submit_batch


@dataclass
class _Run:
    """In-memory state of one running attempt."""

    phase: Phase = "pending"
    cancel_requested: bool = False  # set by TaskRunner.cancel; checked at the cancel points
    interrupted: bool = False  # TaskRunner.cancel cancelled the asyncio task (phase "run")
    task: asyncio.Task[None] | None = None


CANCELED_SUMMARY = "Task canceled."


class TaskRunner:
    def __init__(
        self,
        store: TaskStore,
        provider: LLMProvider,
        catalog: ManifestCatalog,
        fixtures_root: Path,
        context: ContextServiceClient,
        spec: AgentSpec,
        *,
        deadline_grace_s: float = 5.0,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self.spec = spec
        self.tracer = trace.get_tracer(
            f"nexus_{spec.agent_id.value.split('-')[0]}", tracer_provider=tracer_provider
        )
        self.store = store
        self.provider = provider
        self.catalog = catalog
        self.fixtures_root = fixtures_root
        self.context = context
        self.deadline_grace_s = deadline_grace_s
        self._tasks: set[asyncio.Task[None]] = set()
        self._runs: dict[tuple[str, int], _Run] = {}
        self._stopping = False

    def start(self, request: TaskRequest) -> asyncio.Task[None]:
        key = (request.task_id, request.attempt)
        run = _Run()
        self._runs[key] = run
        task = asyncio.create_task(self._guarded(request, run), name=f"task:{request.task_id}")
        run.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _t: self._forget(key, run))
        return task

    def _forget(self, key: tuple[str, int], run: _Run) -> None:
        if self._runs.get(key) is run:
            del self._runs[key]

    def cancel(self, task_id: str) -> None:
        """Cancel the in-flight run(s) of ``task_id``, if any. Every run is flagged (checked at
        the cancel points); only a run inside ``spec.run`` has its asyncio task cancelled, so a
        commit that started is never interrupted."""
        for (tid, _attempt), run in list(self._runs.items()):
            if tid != task_id:
                continue
            run.cancel_requested = True
            if run.phase == "run" and run.task is not None and not run.task.done():
                run.interrupted = True
                run.task.cancel()

    async def drain(self) -> None:
        """Wait for running tasks (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        """Shutdown: cancel in-flight runs. Their rows stay WORKING; the next boot sweeps them."""
        self._stopping = True
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _guarded(self, request: TaskRequest, run: _Run) -> None:
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
                try:
                    await self._execute(request, run)
                except asyncio.CancelledError:
                    if not run.interrupted or self._stopping:
                        raise  # shutdown: the row stays WORKING for the next boot sweep
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
                    log.info("task canceled in flight", extra=_ids(request))
                    await self._finish_latest(request, AgentTaskStatus.CANCELED)
            except Exception:  # also covers a failed CANCELED write: one crash handler
                log.exception("task crashed", extra=_ids(request))
                await self._finish_latest(request, AgentTaskStatus.FAILED, ErrorCode.INTERNAL_ERROR)

    async def _cancel_point(self, row: TaskRow, run: _Run) -> bool:
        """True if a cancel was requested for this attempt (in memory or in the store)."""
        if run.cancel_requested:
            return True
        flagged = await self.store.is_cancel_requested(row["task_id"], row["attempt"])
        return flagged or run.cancel_requested  # re-check: a cancel may land during the read

    async def _execute(self, request: TaskRequest, run: _Run) -> None:
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

        if await self._cancel_point(row, run):  # cancel point (a): before the LLM call
            await self._finish(row, AgentTaskStatus.CANCELED, summary=CANCELED_SUMMARY)
            return

        remaining = (request.deadline - datetime.now(UTC)).total_seconds() + self.deadline_grace_s
        try:
            if remaining <= 0:
                raise TimeoutError
            run.phase = "run"
            outcome = await asyncio.wait_for(
                self.spec.run(
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
            run.phase = "pending"
            await self._finish(
                row,
                AgentTaskStatus.FAILED,
                error=ErrorCode.DEADLINE_EXCEEDED,
                summary="Task failed: deadline_exceeded.",
            )
            return
        run.phase = "pending"

        common = {
            "usage": outcome.usage,
            "model_id": outcome.model_id,
            "prompt_version": outcome.prompt_version,
            "warnings": outcome.warnings,
        }
        if await self._cancel_point(row, run):  # cancel point (b): before the commit
            await self._finish(row, AgentTaskStatus.CANCELED, summary=CANCELED_SUMMARY, **common)
            return
        if outcome.status is not AgentTaskStatus.COMPLETED:
            code = outcome.error.code if outcome.error else ErrorCode.INTERNAL_ERROR
            await self._finish(row, outcome.status, error=code, summary=outcome.summary, **common)
            return
        if outcome.batch is None:  # zero candidates: COMPLETED, no submit (FR-031)
            await self._finish(row, AgentTaskStatus.COMPLETED, summary=outcome.summary, **common)
            return

        run.phase = "commit"  # from here on, a cancel never interrupts the task
        result = await self.context.submit_batch(outcome.batch)
        if result.cancelled:  # 409 investigation_cancelled: the investigation was fenced
            await self._finish(
                row,
                AgentTaskStatus.CANCELED,
                summary="Task canceled: the investigation was cancelled.",
                **common,
            )
            return
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

    async def _finish_latest(
        self, request: TaskRequest, status: AgentTaskStatus, code: ErrorCode | None = None
    ) -> None:
        """End this attempt ``status`` (CANCELED or FAILED) unless it is already terminal."""
        row = await self.store.get(request.task_id, request.attempt)
        if row is not None and not row.status.is_terminal:
            summary = CANCELED_SUMMARY if status is AgentTaskStatus.CANCELED else "Task failed."
            await self._finish(row, status, error=code, summary=summary)


def _ids(request: TaskRequest) -> dict[str, object]:
    return {
        "task_id": request.task_id,
        "attempt": request.attempt,
        "investigation_id": request.investigation_id,
    }
