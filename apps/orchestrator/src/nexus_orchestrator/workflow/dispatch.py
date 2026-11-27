"""Task dispatch: PENDING → DISPATCHED (S2 spec FR-003; main spec §8.2, §8.3; ADR-003, ADR-008 F2).

``PENDING`` is the committed intent. ``POST /v1/tasks`` happens **after** that commit, outside
any transaction; ``DISPATCHED`` + ``acked_at`` is written only once the agent acknowledges.

=====================================  ===========================================================
Agent answer                           Effect
=====================================  ===========================================================
202 SUBMITTED / 200 duplicate          PENDING → DISPATCHED (+ ``acked_at``). A duplicate already
                                       WORKING/COMPLETED/FAILED is still just an ack here; the next
                                       poll reads the full ``TaskResult`` (finding ids, usage).
202/200 REJECTED                       PENDING → FAILED (``task_rejected``)
409 content_conflict / stale_attempt   PENDING → FAILED (that code); not retried
other 4xx (401/403/422 …)              PENDING → FAILED (``task_rejected``): a config error
connect error / timeout / 5xx          stays PENDING. First dispatch of an attempt: retried
                                       ``dispatch_max_retries`` (3) times
                                       ``dispatch_backoff_seconds`` (2 s) apart. Later ticks
                                       (``single_shot``): one try, no loop
retries exhausted                      stays PENDING, logged, ``transition.dropped`` timeline note
                                       (``reason=agent_unreachable``), first dispatch only. The
                                       driver re-POSTs it once per tick until its deadline, then
                                       the deadline watcher times it out (S3 §12, FR-120)
=====================================  ===========================================================

**The row is re-read before every send** (and between retry tries): once it is no longer PENDING
at this attempt (a fence, a cancel, a timeout) nothing more is sent. A late ack is written only if
the row is still at this attempt (version-checked in the write transaction); a row that moved on
gets a best-effort agent cancel instead (orphan).

**Byte-identical resubmits.** The agent dedups on ``(task_id, attempt)`` and answers ``409
content_conflict`` if the body hash differs (S2 FR-020). Every field of the ``TaskRequest`` is
therefore derived from stored state: the task row, the manifest, and the investigation's stored
``trace_context_json`` (the ``workflow.create`` span) as ``trace_context``. The ``a2a.submit``
span is a child of that same context, and the instrumented client puts the HTTP span's
``traceparent`` in the header, so everything stays in one trace; the agent's ``task.run`` becomes
a sibling of ``a2a.submit`` instead of its child. That is the price of a stable body.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable

from opentelemetry import context as otel_context
from opentelemetry import propagate
from opentelemetry.trace import Tracer

from nexus_contracts.a2a.task import AgentTaskStatus, TaskRequest, TraceContext
from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCode
from nexus_contracts.api.states import OrchestratorTaskState
from nexus_contracts.common import Classification, ClassificationRank
from nexus_contracts.events.timeline import TimelineEventType
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_orchestrator.clock import Clock, SystemClock
from nexus_orchestrator.delegation.agent_client import AgentClient, SubmitKind, SubmitOutcome
from nexus_orchestrator.ids import iso
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.repository import (
    Applied,
    InvestigationRecord,
    Repository,
    TaskRecord,
)

log = logging.getLogger(__name__)

CONTRACT_VERSION = "1.0"
_REFUSAL_CODES = frozenset({ErrorCode.CONTENT_CONFLICT, ErrorCode.STALE_ATTEMPT})


class DispatchImpossible(ValueError):
    """The request cannot be built (e.g. the manifest no longer has this agent's task)."""


def stored_carrier(trace_context_json: str | None) -> dict[str, str]:
    if not trace_context_json:
        return {}
    try:
        carrier = json.loads(trace_context_json)
    except ValueError:
        return {}
    return carrier if isinstance(carrier, dict) else {}


def body_trace_context(inv: InvestigationRecord, task_id: str) -> TraceContext:
    carrier = stored_carrier(inv.trace_context_json)
    if carrier.get("traceparent"):
        return TraceContext(
            traceparent=carrier["traceparent"], tracestate=carrier.get("tracestate", "")
        )
    # No stored context (tracing off at create): a deterministic, unsampled traceparent.
    digest = hashlib.sha256(task_id.encode()).hexdigest()
    return TraceContext(traceparent=f"00-{digest[:32]}-{digest[32:48]}-00")


def build_request(
    inv: InvestigationRecord, task: TaskRecord, manifest: ScenarioManifest
) -> TaskRequest:
    template = next((t for t in manifest.template.tasks if t.agent is task.agent_id), None)
    if template is None:
        raise DispatchImpossible(f"{manifest.scenario_id} has no template task for {task.agent_id}")
    evidence = {e.id: e for e in manifest.evidence}
    # The ceiling is the highest classification among the task's own evidence, so the agent can
    # use all of it (FR-021: ceiling ≥ evidence used); DEMO-LOW when there is none.
    ranks = [ClassificationRank[evidence[r].classification.name] for r in template.input_refs]
    ceiling = Classification[ClassificationRank(max(ranks, default=1)).name]
    return TaskRequest(
        task_id=task.id,
        investigation_id=inv.id,
        task_type=task.task_type,
        objective=inv.objective,
        input_refs=list(template.input_refs),
        scenario_id=inv.scenario_id,
        classification_ceiling=ceiling,
        attempt=task.attempt,
        token_budget=task.token_reservation,
        deadline=task.deadline,
        contract_version=CONTRACT_VERSION,
        trace_context=body_trace_context(inv, task.id),
    )


class Dispatcher:
    def __init__(
        self,
        settings: OrchestratorSettings,
        db: Database,
        repo: Repository,
        agents: AgentClient,
        tracer: Tracer,
        is_leader: Callable[[], bool],
        clock: Clock | None = None,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self.settings = settings
        self.db = db
        self.repo = repo
        self.agents = agents
        self.tracer = tracer
        self.is_leader = is_leader
        self.backing_off = 0  # observability for tests: dispatches sleeping between retry tries

    async def dispatch(
        self,
        inv: InvestigationRecord,
        task: TaskRecord,
        manifest: ScenarioManifest,
        *,
        single_shot: bool = False,
    ) -> SubmitOutcome | None:
        """Submit one PENDING task: the FR-003 retry loop, or one try when ``single_shot`` (the
        per-tick re-POST after the first dispatch). Before each send the row is re-read and the
        dispatch stops if it is no longer PENDING at ``task.attempt``. Returns the last outcome,
        or ``None`` if nothing was sent (not leader, request impossible, row moved on)."""
        log_extra = {
            "investigation_id": inv.id,
            "task_id": task.id,
            "attempt": task.attempt,
            "agent_id": task.agent_id.value,
        }
        try:
            request = build_request(inv, task, manifest)
        except DispatchImpossible:
            log.exception("cannot build TaskRequest; task stays PENDING", extra=log_extra)
            return None
        attempts = 1 if single_shot else 1 + self.settings.dispatch_max_retries
        parent = propagate.extract(stored_carrier(inv.trace_context_json))
        outcome: SubmitOutcome | None = None
        for n in range(1, attempts + 1):
            if not self.is_leader():
                log.warning("not the leader; dispatch skipped", extra=log_extra)
                return outcome
            current = await self.repo.task_record(task.id)
            if (
                current is None
                or current.status is not OrchestratorTaskState.PENDING
                or current.attempt != task.attempt
            ):
                log.info(
                    "task no longer pending at this attempt; dispatch stopped",
                    extra={**log_extra, "status": current.status.value if current else None},
                )
                return outcome
            task = current  # the current version for the ack / failure write
            outcome = await self._submit_once(request, task, parent, n)
            if outcome.kind is SubmitKind.ACKED:
                await self._acked(task, outcome)
                return outcome
            if outcome.kind is SubmitKind.REFUSED:
                code = outcome.code if outcome.code in _REFUSAL_CODES else ErrorCode.TASK_REJECTED
                log.error(
                    "agent refused the task",
                    extra={**log_extra, "detail": outcome.detail, "code": code.value},
                )
                await self._fail(task, code)
                return outcome
            if single_shot:
                # Once per tick while the agent is unreachable: no timeline note (the first
                # dispatch wrote one), and debug only, not a warning every 2 s.
                log.debug(
                    "re-POST failed; task stays PENDING",
                    extra={**log_extra, "detail": outcome.detail},
                )
                return outcome
            log.warning(
                "dispatch failed; task stays PENDING",
                extra={**log_extra, "detail": outcome.detail, "try": n, "of": attempts},
            )
            if n < attempts:
                self.backing_off += 1
                try:
                    await self.clock.sleep(self.settings.dispatch_backoff_seconds)
                finally:
                    self.backing_off -= 1
        await self._unreachable(task, attempts, outcome)
        return outcome

    async def _submit_once(
        self, request: TaskRequest, task: TaskRecord, parent: otel_context.Context, n: int
    ) -> SubmitOutcome:
        attrs = {
            "nexus.investigation.id": request.investigation_id,
            "nexus.task.id": task.id,
            "nexus.task.attempt": task.attempt,
            "nexus.agent.id": task.agent_id.value,
            "nexus.dispatch.try": n,
        }
        with self.tracer.start_as_current_span("a2a.submit", context=parent, attributes=attrs) as s:
            outcome = await self.agents.submit(task.agent_id, request)
            s.set_attribute("nexus.dispatch.outcome", outcome.kind.value)
            if outcome.status is not None:
                s.set_attribute("nexus.agent.status", outcome.status.value)
        return outcome

    async def _acked(self, task: TaskRecord, outcome: SubmitOutcome) -> None:
        if outcome.status is AgentTaskStatus.REJECTED:
            await self._fail(task, ErrorCode.TASK_REJECTED)
            return
        now = self.clock.now()
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT attempt FROM tasks WHERE id = ?", (task.id,))
            stale = row is None or row["attempt"] != task.attempt
            if not stale:
                applied = await self.repo.transition_task(
                    tx,
                    task,
                    OrchestratorTaskState.DISPATCHED,
                    now=now,
                    fields={"acked_at": iso(now)},
                )
        if stale:
            # A later attempt owns the row: this ack is for an attempt that is over. No cancel
            # (the agent cancel is by task id and would stop the new attempt too); the agent
            # refuses the old attempt's commit as stale (FR-122).
            log.warning(
                "ack for an attempt that is over; ignored",
                extra={"task_id": task.id, "attempt": task.attempt},
            )
            return
        if applied is Applied.DROPPED:
            # The row moved on while the submit was in flight (cancelled by the fence, or timed
            # out), so nobody else knows the agent now holds this task: stop it ourselves.
            await self._cancel_orphan(task)

    async def _cancel_orphan(self, task: TaskRecord) -> None:
        log.warning(
            "task moved on while its submit was in flight; cancelling it at the agent",
            extra={"task_id": task.id, "agent_id": task.agent_id.value},
        )
        try:
            await self.agents.cancel(task.agent_id, task.id)  # best effort, never raises
        except Exception:
            log.warning("orphan cancel failed", extra={"task_id": task.id}, exc_info=True)

    async def _fail(self, task: TaskRecord, code: ErrorCode) -> None:
        async with self.db.transaction() as tx:
            await self.repo.transition_task(
                tx,
                task,
                OrchestratorTaskState.FAILED,
                now=self.clock.now(),
                reason=code,
                fields={"error_code": code.value, "error_category": CATEGORY_BY_CODE[code].value},
            )

    async def _unreachable(
        self, task: TaskRecord, attempts: int, outcome: SubmitOutcome | None
    ) -> None:
        log.error(
            "agent unreachable after all dispatch retries; task stays PENDING (S2 G13)",
            extra={"task_id": task.id, "agent_id": task.agent_id.value, "tries": attempts},
        )
        async with self.db.transaction() as tx:
            await self.repo.append_note(
                tx,
                task.investigation_id,
                TimelineEventType.TRANSITION_DROPPED,
                {
                    "entity": "task",
                    "id": task.id,
                    "current_state": OrchestratorTaskState.PENDING.value,
                    "attempted_state": OrchestratorTaskState.DISPATCHED.value,
                    "reason": "agent_unreachable",
                    "agent_id": task.agent_id.value,
                    "tries": attempts,
                    "detail": outcome.detail if outcome else None,
                },
                self.clock.now(),
            )
