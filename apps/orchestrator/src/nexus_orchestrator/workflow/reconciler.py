"""Close reconciler (S3b FR-126; ADR-002 A2).

Plain version: the Context Service must be told when an investigation has ended, so it stops
accepting findings for it. Some endings tell it inline (cancel, investigation deadline) and some
do not (a normal PARTIAL/FAILED finish), and an inline close can fail. This loop is the safety
net: at startup and then every ``reconcile_interval_seconds`` it looks for terminal investigations
with ``context_closed = 0`` and closes each one, one after another. A successful close (the
Context Service's idempotent 200/404/409) flips ``context_closed`` to 1 in its own short
transaction; a failed one leaves 0 and is retried on the next pass.

Only the leader runs a pass. A failure on one investigation never stops the others, and the loop
catches and logs everything except cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from opentelemetry.trace import Tracer

from nexus_contracts.api.states import InvestigationStatus
from nexus_orchestrator.clock import Clock
from nexus_orchestrator.delegation.context_client import ContextServiceClient
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.repository import Repository

log = logging.getLogger(__name__)


class CloseReconciler:
    def __init__(
        self,
        settings: OrchestratorSettings,
        repo: Repository,
        db: Database,
        context: ContextServiceClient,
        tracer: Tracer,
        clock: Clock,
        is_leader: Callable[[], bool],
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.db = db
        self.context = context
        self.tracer = tracer
        self.clock = clock
        self.is_leader = is_leader
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> int:
        """One pass. Returns how many investigations were closed. Skipped when not the leader."""
        if not self.is_leader():
            return 0
        pending = await self.repo.list_unclosed_terminal()
        closed = 0
        for investigation_id, status in pending:
            if await self._close_one(investigation_id, status):
                closed += 1
        if pending:
            log.info("close reconciler pass", extra={"unclosed": len(pending), "closed": closed})
        return closed

    async def _close_one(self, investigation_id: str, status: InvestigationStatus) -> bool:
        with self.tracer.start_as_current_span(
            "workflow.reconcile_close",
            attributes={
                "nexus.investigation.id": investigation_id,
                "nexus.investigation.status": status.value,
            },
        ) as span:
            timeout = self.settings.context_service_timeout_seconds
            try:
                # wait_for as well as the client timeout: a Context Service that is up but stuck
                # must not stall the pass for the investigations behind it.
                ok = await asyncio.wait_for(
                    self.context.close(investigation_id, status, timeout=timeout), timeout
                )
                if ok:
                    async with self.db.transaction() as tx:
                        await self.repo.set_context_closed(
                            tx, investigation_id, True, self.clock.now()
                        )
            except Exception:
                log.warning(
                    "reconcile close failed",
                    extra={"investigation_id": investigation_id},
                    exc_info=True,
                )
                ok = False
            span.set_attribute("nexus.reconcile.closed", ok)
            if not ok:
                log.info(
                    "Context Service close not confirmed; will retry",
                    extra={"investigation_id": investigation_id, "status": status.value},
                )
            return ok

    # -- loop ---------------------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="close-reconciler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("close reconciler pass failed")
            await self.clock.sleep(self.settings.reconcile_interval_seconds)
