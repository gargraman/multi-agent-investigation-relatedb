"""Investigation creation, start and the resume sweep (spec §8.1 steps 1-4, §8.6; ADR-002 A1;
S2 spec FR-002, FR-008).

1. Insert ``CREATED`` (with its timeline event, one transaction).
2. Register with the Context Service **after** commit. If that fails the status stays
   ``CREATED``; the create is idempotent on ``investigation_id``, so the startup sweep and a
   background retrier simply re-POST it.
3. Once registered, **start** in one transaction (FR-002, G1): ``CREATED → RUNNING`` with
   ``skipped_agents`` (template agents not in ``NEXUS_ENABLED_AGENTS``) on the event, then one
   ``PENDING`` task row (attempt 1) per enabled template task, each with its own
   ``task.status_changed``. With no enabled template agent the same transaction ends it
   ``RUNNING → FAILED (agent_not_enabled)`` (``workflow/terminal.py``).
4. Hand the investigation to the poller, which dispatches and polls (``workflow/poller.py``).

Resume sweep (§8.6, ADR-008 F1): ``CREATED`` → re-register → start; ``RUNNING`` → a new driver
that re-POSTs ``PENDING`` tasks (same attempt) and polls ``DISPATCHED``/``RUNNING`` ones.

E4 (FR-008): while any enabled agent's card says ``llm_mode=replay``, an objective other than the
template's is refused (``ReplayObjectiveLocked`` → ``422 replay_objective_locked``).

Tracing (ADR-008 F9): one ``workflow.create`` span per create. Its context is stored in
``investigations.trace_context_json``; resume spans link back to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from datetime import timedelta

from opentelemetry import propagate, trace
from opentelemetry.trace import Link, Tracer

from nexus_contracts.api.context import ContextInvestigationCreate
from nexus_contracts.api.investigations import Investigation, InvestigationCreateRequest
from nexus_contracts.api.states import InvestigationStatus
from nexus_contracts.events.timeline import InvestigationStatusChanged, TimelineEventType
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_orchestrator.clock import Clock, SystemClock
from nexus_orchestrator.delegation.agent_registry import AgentRegistry
from nexus_orchestrator.delegation.context_client import (
    ContextServiceClient,
    RegistrationOutcome,
    RegistrationResult,
)
from nexus_orchestrator.ids import new_investigation_id, new_task_id
from nexus_orchestrator.scenarios import ScenarioCatalog
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.repository import Repository
from nexus_orchestrator.workflow import terminal
from nexus_orchestrator.workflow.poller import Poller

log = logging.getLogger(__name__)


class ScenarioNotFound(LookupError):
    pass


class ConcurrencyLimitReached(RuntimeError):
    pass


class ReplayObjectiveLocked(ValueError):
    pass


class InvestigationWorkflow:
    def __init__(
        self,
        settings: OrchestratorSettings,
        db: Database,
        repo: Repository,
        catalog: ScenarioCatalog,
        context: ContextServiceClient,
        tracer: Tracer,
        poller: Poller,
        registry: AgentRegistry,
        clock: Clock | None = None,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self.settings = settings
        self.db = db
        self.repo = repo
        self.catalog = catalog
        self.context = context
        self.tracer = tracer
        self.poller = poller
        self.registry = registry
        self.pending_registration: set[str] = set()
        self._retry_task: asyncio.Task[None] | None = None

    # -- create -------------------------------------------------------------------------------

    async def create(self, request: InvestigationCreateRequest) -> Investigation:
        manifest = await self.catalog.get(request.scenario_id)
        if manifest is None:
            raise ScenarioNotFound(request.scenario_id)
        if (
            request.objective is not None
            and request.objective != manifest.objective
            and self.registry.replay_locked
        ):
            raise ReplayObjectiveLocked(
                "an agent is in replay mode: only the scenario's template objective can be used"
            )
        objective = request.objective or manifest.objective
        investigation_id = new_investigation_id()
        now = self.clock.now()
        attrs = {
            "nexus.investigation.id": investigation_id,
            "nexus.scenario.id": manifest.scenario_id,
        }

        with self.tracer.start_as_current_span("workflow.create", attributes=attrs) as span:
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            async with self.db.transaction() as tx:
                active = await self.repo.count_active(tx)
                if active >= self.settings.nexus_max_concurrent_investigations:
                    raise ConcurrencyLimitReached(
                        f"{active} investigations in progress; the limit is "
                        f"{self.settings.nexus_max_concurrent_investigations}"
                    )
                investigation = await self.repo.insert_investigation(
                    tx,
                    investigation_id=investigation_id,
                    scenario_id=manifest.scenario_id,
                    objective=objective,
                    token_budget=self.settings.nexus_max_tokens_per_investigation,
                    deadline=now + timedelta(seconds=self.settings.investigation_deadline_seconds),
                    trace_context_json=json.dumps(carrier) if carrier else None,
                    now=now,
                )
                payload = InvestigationStatusChanged(
                    from_status=None, to_status=InvestigationStatus.CREATED
                ).model_dump(mode="json")
                await self.repo.append_event(
                    tx,
                    investigation_id,
                    TimelineEventType.INVESTIGATION_STATUS_CHANGED,
                    payload,
                    now,
                )
            span.set_attribute("nexus.investigation.status", InvestigationStatus.CREATED.value)
            # After commit, outside any transaction (ADR-002 A3).
            result = await self._register(investigation_id, manifest, objective)
            span.set_attribute("nexus.context.registration", result.outcome.value)
        return await self.repo.get_investigation(investigation_id) or investigation

    async def _register(
        self, investigation_id: str, manifest: ScenarioManifest, objective: str
    ) -> RegistrationResult:
        body = ContextInvestigationCreate.from_manifest(investigation_id, manifest, objective)
        result = await self.context.create_investigation(body)
        extra = {
            "investigation_id": investigation_id,
            "outcome": result.outcome.value,
            "detail": result.detail,
            "code": result.code,
        }
        if result.outcome is RegistrationOutcome.REGISTERED:
            self.pending_registration.discard(investigation_id)
            log.info("investigation registered with Context Service", extra=extra)
            if not await self.begin(investigation_id, manifest):
                await self._close_late_registration(investigation_id)
        elif result.retryable:
            self.pending_registration.add(investigation_id)
            log.warning("Context Service create failed; will retry", extra=extra)
        else:
            # Permanent (e.g. 409 content_conflict): retrying the same body cannot succeed.
            self.pending_registration.discard(investigation_id)
            log.error("Context Service rejected investigation create", extra=extra)
        return result

    # -- begin (FR-002, G1) --------------------------------------------------------------------

    async def _close_late_registration(self, investigation_id: str) -> None:
        """The Context Service create landed, but the investigation had already ended (cancelled
        while the create was in flight). The fence's close saw nothing registered (404, which
        counts as closed, so ``context_closed = 1`` and FR-126 never revisits it); close the
        record that has just appeared. Best effort: log and carry on."""
        record = await self.repo.investigation_record(investigation_id)
        if record is None or not record.status.is_terminal:
            return
        try:
            closed = await self.context.close(
                investigation_id,
                record.status,
                timeout=self.settings.context_service_timeout_seconds,
            )
        except Exception:
            closed = False
            log.exception(
                "late Context Service close failed", extra={"investigation_id": investigation_id}
            )
        if not closed:
            log.warning(
                "could not close a late-registered Context Service record",
                extra={"investigation_id": investigation_id, "status": record.status.value},
            )

    async def begin(self, investigation_id: str, manifest: ScenarioManifest) -> bool:
        """CREATED → RUNNING plus the task rows, in one transaction; then hand to the poller.
        False when it was not CREATED any more (started by a racing retrier, or already ended)."""
        enabled = set(self.settings.nexus_enabled_agents)
        now = self.clock.now()
        async with self.db.transaction() as tx:
            inv = await self.repo.tx_investigation_record(tx, investigation_id)
            if inv is None or inv.status is not InvestigationStatus.CREATED:
                return False  # already started (e.g. a racing retrier) or ended
            skipped = [t.agent for t in manifest.template.tasks if t.agent not in enabled]
            await self.repo.transition_investigation(
                tx, investigation_id, InvestigationStatus.RUNNING, now=now, skipped_agents=skipped
            )
            deadline = min(
                now + timedelta(seconds=self.settings.task_deadline_seconds), inv.deadline
            )
            for template in manifest.template.tasks:
                if template.agent not in enabled:
                    continue
                await self.repo.insert_task(
                    tx,
                    task_id=new_task_id(),
                    investigation_id=investigation_id,
                    agent_id=template.agent,
                    task_type=template.task_type,
                    deadline=deadline,
                    # §8.5 / ADR-008 F5: share × cap (SIGINT 0.25 × 60000 = 15000).
                    token_reservation=math.floor(
                        template.token_share * self.settings.nexus_max_tokens_per_investigation
                    ),
                    now=now,
                )
            tasks = await self.repo.tx_task_records(tx, investigation_id)
            decision = terminal.decide(
                [terminal.TaskOutcome(t.agent_id, t.status) for t in tasks], skipped
            )
            if decision is not None:  # only when no template agent is enabled
                await self.repo.transition_investigation(
                    tx, investigation_id, decision.status, now=now, reason=decision.reason
                )
        log.info(
            "investigation started",
            extra={
                "investigation_id": investigation_id,
                "tasks": len(tasks),
                "skipped_agents": [a.value for a in skipped],
            },
        )
        if decision is None:
            self.poller.ensure(investigation_id)
        return True

    # -- resume (ADR-002 A1) ------------------------------------------------------------------

    async def resume_sweep(self) -> dict[str, int]:
        """Re-POST the idempotent create for every ``CREATED`` investigation (and start it once
        registered); start a driver for every ``RUNNING`` one. Never raises for Context Service
        or agent failures: those go to the background retrier / the drivers."""
        ids = await self.repo.list_ids_with_status(InvestigationStatus.CREATED)
        manifests = await self.catalog.all()
        counts = {o.value: 0 for o in RegistrationOutcome} | {"skipped": 0}
        for investigation_id in ids:
            outcome = await self._resume_one(investigation_id, manifests)
            counts[outcome] += 1
        running = await self.repo.list_ids_with_status(InvestigationStatus.RUNNING)
        for investigation_id in running:
            self.poller.ensure(investigation_id)
        counts["running_resumed"] = len(running)
        log.info("resume sweep finished", extra={"investigations": len(ids), **counts})
        return counts

    async def _resume_one(
        self, investigation_id: str, manifests: dict[str, ScenarioManifest]
    ) -> str:
        investigation = await self.repo.get_investigation(investigation_id)
        if investigation is None or investigation.status is not InvestigationStatus.CREATED:
            self.pending_registration.discard(investigation_id)
            return "skipped"
        manifest = manifests.get(investigation.scenario_id)
        if manifest is None:
            self.pending_registration.discard(investigation_id)
            log.error(
                "cannot resume: scenario manifest missing",
                extra={
                    "investigation_id": investigation_id,
                    "scenario_id": investigation.scenario_id,
                },
            )
            return "skipped"
        links = self._links(await self.repo.get_trace_context(investigation_id))
        attrs = {
            "nexus.investigation.id": investigation_id,
            "nexus.scenario.id": investigation.scenario_id,
        }
        with self.tracer.start_as_current_span("workflow.resume", links=links, attributes=attrs):
            result = await self._register(investigation_id, manifest, investigation.objective)
        return result.outcome.value

    @staticmethod
    def _links(trace_context_json: str | None) -> list[Link]:
        if not trace_context_json:
            return []
        try:
            carrier = json.loads(trace_context_json)
        except ValueError:
            return []
        ctx = trace.get_current_span(propagate.extract(carrier)).get_span_context()
        return [Link(ctx)] if ctx.is_valid else []

    # -- background retrier -------------------------------------------------------------------

    def start(self) -> None:
        self._retry_task = asyncio.create_task(self._retry_loop(), name="context-create-retrier")

    async def stop(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._retry_task
            self._retry_task = None

    async def _retry_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.context_retry_interval_seconds)
            if not self.pending_registration:
                continue
            try:
                manifests = await self.catalog.all()
                for investigation_id in sorted(self.pending_registration):
                    await self._resume_one(investigation_id, manifests)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Context Service create retry pass failed")
