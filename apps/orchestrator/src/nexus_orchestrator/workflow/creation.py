"""Investigation creation and the S1 resume sweep (spec §8.1 steps 1-3, §8.6; ADR-002 A1).

S1 scope: no tasks and no workflow engine yet. An investigation is inserted ``CREATED`` (with its
timeline event, one transaction), then registered with the Context Service **after** commit.
Whether or not that call succeeds the status stays ``CREATED``; the Context Service create is
idempotent on ``investigation_id``, so the startup sweep (and a background retrier for transient
failures) simply re-POSTs it.

Tracing (ADR-008 F9): one ``workflow.create`` span per create. Its context is stored in
``investigations.trace_context_json``; resume spans link back to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import timedelta

from opentelemetry import propagate, trace
from opentelemetry.trace import Link, Tracer

from nexus_contracts.api.context import ContextInvestigationCreate
from nexus_contracts.api.investigations import Investigation, InvestigationCreateRequest
from nexus_contracts.api.states import InvestigationStatus
from nexus_contracts.events.timeline import InvestigationStatusChanged, TimelineEventType
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_orchestrator.delegation.context_client import (
    ContextServiceClient,
    RegistrationOutcome,
    RegistrationResult,
)
from nexus_orchestrator.ids import new_investigation_id, utcnow
from nexus_orchestrator.scenarios import ScenarioCatalog
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.repository import Repository

log = logging.getLogger(__name__)


class ScenarioNotFound(LookupError):
    pass


class ConcurrencyLimitReached(RuntimeError):
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
    ) -> None:
        self.settings = settings
        self.db = db
        self.repo = repo
        self.catalog = catalog
        self.context = context
        self.tracer = tracer
        self.pending_registration: set[str] = set()
        self._retry_task: asyncio.Task[None] | None = None

    # -- create -------------------------------------------------------------------------------

    async def create(self, request: InvestigationCreateRequest) -> Investigation:
        manifest = await self.catalog.get(request.scenario_id)
        if manifest is None:
            raise ScenarioNotFound(request.scenario_id)
        objective = request.objective or manifest.objective
        investigation_id = new_investigation_id()
        now = utcnow()
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
        return investigation

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
        elif result.retryable:
            self.pending_registration.add(investigation_id)
            log.warning("Context Service create failed; will retry", extra=extra)
        else:
            # Permanent (e.g. 409 content_conflict): retrying the same body cannot succeed.
            self.pending_registration.discard(investigation_id)
            log.error("Context Service rejected investigation create", extra=extra)
        return result

    # -- resume (ADR-002 A1) ------------------------------------------------------------------

    async def resume_sweep(self) -> dict[str, int]:
        """Re-POST the idempotent create for every ``CREATED`` investigation. Never raises for
        Context Service failures: those go to the background retrier."""
        ids = await self.repo.list_ids_with_status(InvestigationStatus.CREATED)
        manifests = await self.catalog.all()
        counts = {o.value: 0 for o in RegistrationOutcome} | {"skipped": 0}
        for investigation_id in ids:
            outcome = await self._resume_one(investigation_id, manifests)
            counts[outcome] += 1
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
