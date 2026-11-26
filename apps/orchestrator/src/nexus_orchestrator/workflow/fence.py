"""Fence-first terminal routine (S3b FR-123, FR-125 steps 1-4; ADR-008 F6).

Plain version: when an investigation is ended from outside the normal finish path (its deadline
passed, or a user cancelled it), three parties must stop: the database row, the Context Service
(so no agent can still write findings into a finished investigation) and the agents themselves.
The order matters. The row flips first, so nothing new starts; the Context Service is closed
second, so a straggling agent's writes are refused; only then are the agents told to stop. A step
that fails never blocks the next one.

1. One transaction: investigation -> ``status`` (version guarded) and ``context_closed = 0``.
   Anything but APPLIED means someone else already ended it: return, touch nothing else.
   The non-terminal tasks are read in this same transaction, so steps 3-4 act on exactly the
   tasks that were open when the investigation was ended.
2. Context Service close, bounded by ``cancel_step_timeout_seconds``. On success
   ``context_closed = 1``. A failure leaves 0 for the reconciler.
3. Agent cancel for those tasks, concurrently, bounded by the same timeout as a whole.
4. Those tasks -> CANCELLED (version guarded: a task that finished meanwhile is DROPPED).

``end`` never raises for a network failure in steps 2-3 (it logs and carries on).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from opentelemetry.trace import Tracer

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus, OrchestratorTaskState
from nexus_orchestrator.clock import Clock
from nexus_orchestrator.delegation.agent_client import AgentClient
from nexus_orchestrator.delegation.context_client import ContextServiceClient
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.repository import Applied, Repository, TaskRecord

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FenceResult:
    applied: bool  # this call moved the investigation to ``status``
    status: InvestigationStatus  # the status stored after step 1 (the new one, or the current one)
    context_closed: bool  # ``investigations.context_closed`` after the routine


class Fence:
    def __init__(
        self,
        settings: OrchestratorSettings,
        db: Database,
        repo: Repository,
        context: ContextServiceClient,
        agents: AgentClient,
        tracer: Tracer,
        clock: Clock,
    ) -> None:
        self.settings = settings
        self.db = db
        self.repo = repo
        self.context = context
        self.agents = agents
        self.tracer = tracer
        self.clock = clock

    async def end(
        self,
        investigation_id: str,
        status: InvestigationStatus,
        reason: str | None,
        *,
        task_reason: str | None,
    ) -> FenceResult:
        """Run steps 1-4 for ``investigation_id``. ``reason`` / ``task_reason`` are
        :class:`ErrorCode` values (``None`` for none), written to the investigation and to every
        task this call cancels."""
        reason_code = ErrorCode(reason) if reason else None
        task_reason_code = ErrorCode(task_reason) if task_reason else None
        with self.tracer.start_as_current_span(
            "workflow.fence",
            attributes={
                "nexus.investigation.id": investigation_id,
                "nexus.investigation.status": status.value,
            },
        ) as span:
            result, open_tasks = await self._step1(investigation_id, status, reason_code)
            span.set_attribute("nexus.fence.applied", result.applied)
            if not result.applied:
                return result
            closed = await self._close_context(investigation_id, status)
            await self._cancel_agents(open_tasks)
            await self._cancel_tasks(open_tasks, task_reason_code)
            span.set_attribute("nexus.fence.context_closed", closed)
            return FenceResult(True, status, closed)

    async def _step1(
        self, investigation_id: str, status: InvestigationStatus, reason: ErrorCode | None
    ) -> tuple[FenceResult, list[TaskRecord]]:
        async with self.db.transaction() as tx:
            now = self.clock.now()
            applied = await self.repo.transition_investigation(
                tx, investigation_id, status, now=now, reason=reason
            )
            if applied is not Applied.APPLIED:
                current = await self.repo.tx_investigation_record(tx, investigation_id)
                assert current is not None, investigation_id
                return FenceResult(False, current.status, current.context_closed), []
            await self.repo.set_context_closed(tx, investigation_id, False, now)
            tasks = await self.repo.tx_task_records(tx, investigation_id)
        open_tasks = [t for t in tasks if not t.status.is_terminal]
        return FenceResult(True, status, False), open_tasks

    async def _close_context(self, investigation_id: str, status: InvestigationStatus) -> bool:
        timeout = self.settings.cancel_step_timeout_seconds
        try:
            closed = await asyncio.wait_for(
                self.context.close(investigation_id, status, timeout=timeout), timeout
            )
        except Exception:
            log.warning(
                "Context Service close failed",
                extra={"investigation_id": investigation_id},
                exc_info=True,
            )
            return False
        if not closed:
            log.warning(
                "Context Service did not confirm the close",
                extra={"investigation_id": investigation_id},
            )
            return False
        try:
            async with self.db.transaction() as tx:
                await self.repo.set_context_closed(tx, investigation_id, True, self.clock.now())
        except Exception:
            log.exception(
                "could not record context_closed", extra={"investigation_id": investigation_id}
            )
            return False
        return True

    async def _cancel_agents(self, tasks: list[TaskRecord]) -> None:
        if not tasks:
            return
        timeout = self.settings.cancel_step_timeout_seconds
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(self.agents.cancel(t.agent_id, t.id) for t in tasks), return_exceptions=True
                ),
                timeout,
            )
        except TimeoutError:
            log.warning(
                "agent cancels timed out",
                extra={"investigation_id": tasks[0].investigation_id, "tasks": len(tasks)},
            )

    async def _cancel_tasks(self, tasks: list[TaskRecord], reason: ErrorCode | None) -> None:
        if not tasks:
            return
        async with self.db.transaction() as tx:
            now = self.clock.now()
            for task in tasks:
                await self.repo.transition_task(
                    tx, task, OrchestratorTaskState.CANCELLED, now=now, reason=reason
                )
