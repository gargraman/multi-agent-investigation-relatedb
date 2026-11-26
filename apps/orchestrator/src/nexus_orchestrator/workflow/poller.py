"""Per-investigation driver: dispatch, poll, finish (S2 spec FR-003..FR-007; main spec §8.3, §8.7).

One asyncio task per RUNNING investigation (§8.7). It is stateless: every tick re-reads the
rows, so a restart (resume sweep) just starts a new driver.

Each tick, only while this process **holds the leader lock** (ADR-005 D1):

1. On the driver's first leader tick, dispatch every PENDING task (same ``attempt``; the agent's
   ``(task_id, attempt)`` dedup makes a resume re-POST safe, ADR-008 F2).
2. Poll every DISPATCHED/RUNNING task through the uninstrumented client and map the result with
   ``status_mapping`` (the only agent↔orchestrator crossing, §8.2). A poll that changes nothing
   writes nothing: no event and no span per poll (F9).
3. When every task row is terminal, apply the S2 terminal rule (``workflow/terminal.py``) and
   exit.

Then sleep ``poll_interval_seconds`` ± ``poll_jitter_ratio`` (2 s ± 20 %).

On a transition to a terminal state the result is persisted with it in one transaction:
``finding_ids_json`` (COMPLETED), ``usage_json``, ``llm_mode``, ``error_category``/``error_code``
(FAILED), ``tokens_used += input + output`` and, if ``usage.replayed``, an ``llm.replayed`` event
(FR-006). Usage reported by a FAILED attempt is added too: it is real spend (§8.5 "the
orchestrator adds these up").
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Callable

from opentelemetry import propagate
from opentelemetry.trace import Tracer

from nexus_contracts.a2a.status_mapping import (
    PollFailure,
    TaskStateUpdate,
    map_poll_outcome,
    map_task_result,
)
from nexus_contracts.a2a.task import TaskResult
from nexus_contracts.api.errors import CATEGORY_BY_CODE
from nexus_contracts.api.states import InvestigationStatus, OrchestratorTaskState
from nexus_contracts.common import AgentId
from nexus_contracts.events.timeline import TimelineEventType
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_orchestrator.clock import Clock, SystemClock
from nexus_orchestrator.delegation.agent_client import AgentClient
from nexus_orchestrator.scenarios import ScenarioCatalog
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.repository import Applied, Repository, TaskRecord
from nexus_orchestrator.workflow import terminal
from nexus_orchestrator.workflow.dispatch import Dispatcher, stored_carrier

log = logging.getLogger(__name__)

POLLED_STATES = frozenset({OrchestratorTaskState.DISPATCHED, OrchestratorTaskState.RUNNING})


def skipped_agents(manifest: ScenarioManifest | None, tasks: list[TaskRecord]) -> list[AgentId]:
    """Template agents without a task row: the ones not enabled when the tasks were created."""
    if manifest is None:
        return []
    have = {t.agent_id for t in tasks}
    return [t.agent for t in manifest.template.tasks if t.agent not in have]


def outcomes_in_template_order(
    manifest: ScenarioManifest | None, tasks: list[TaskRecord]
) -> list[terminal.TaskOutcome]:
    """Task outcomes ordered by their agent's manifest-template index (FR-124); a task whose
    agent is not in the template goes last. ``retry_open`` is wired in by the retry task (S3b-7)."""
    order = [t.agent for t in manifest.template.tasks] if manifest is not None else []
    rank = {agent: i for i, agent in enumerate(order)}
    ordered = sorted(tasks, key=lambda t: rank.get(t.agent_id, len(order)))  # stable
    return [terminal.TaskOutcome(t.agent_id, t.status, t.status_reason, False) for t in ordered]


class Poller:
    def __init__(
        self,
        settings: OrchestratorSettings,
        db: Database,
        repo: Repository,
        catalog: ScenarioCatalog,
        agents: AgentClient,
        dispatcher: Dispatcher,
        tracer: Tracer,
        is_leader: Callable[[], bool],
        clock: Clock | None = None,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self.settings = settings
        self.db = db
        self.repo = repo
        self.catalog = catalog
        self.agents = agents
        self.dispatcher = dispatcher
        self.tracer = tracer
        self.is_leader = is_leader
        self._drivers: dict[str, asyncio.Task[None]] = {}
        self.polls = 0  # observability for tests: number of GET /v1/tasks issued
        self._stopped = False

    # -- lifecycle ------------------------------------------------------------------------------

    def ensure(self, investigation_id: str) -> None:
        """Start a driver for this investigation unless one is already running."""
        if self._stopped:
            return
        current = self._drivers.get(investigation_id)
        if current is not None and not current.done():
            return
        self._drivers[investigation_id] = asyncio.create_task(
            self._drive(investigation_id), name=f"driver-{investigation_id}"
        )

    def active(self) -> set[str]:
        return {i for i, t in self._drivers.items() if not t.done()}

    async def stop(self) -> None:
        """Graceful shutdown (ADR-005 D5): cancel drivers, change no state."""
        self._stopped = True
        tasks = list(self._drivers.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._drivers.clear()

    def _sleep_seconds(self) -> float:
        base = self.settings.poll_interval_seconds
        jitter = self.settings.poll_jitter_ratio
        return base * random.uniform(1 - jitter, 1 + jitter)

    # -- driver ---------------------------------------------------------------------------------

    async def _drive(self, investigation_id: str) -> None:
        manifest: ScenarioManifest | None = None
        dispatched = False
        while True:
            try:
                if self.is_leader():
                    inv = await self.repo.investigation_record(investigation_id)
                    if inv is None or inv.status is not InvestigationStatus.RUNNING:
                        return
                    if manifest is None:
                        manifest = await self.catalog.get(inv.scenario_id)
                        if manifest is None:
                            log.error(
                                "scenario manifest missing; cannot dispatch",
                                extra={"investigation_id": investigation_id},
                            )
                    tasks = await self.repo.task_records(investigation_id)
                    if not dispatched and manifest is not None:
                        pending = [t for t in tasks if t.status is OrchestratorTaskState.PENDING]
                        await asyncio.gather(
                            *(self.dispatcher.dispatch(inv, t, manifest) for t in pending)
                        )
                        dispatched = self.is_leader()  # lost it mid-way: retry next tick
                        tasks = await self.repo.task_records(investigation_id)
                    polled = [t for t in tasks if t.status in POLLED_STATES]
                    await asyncio.gather(
                        *(self._poll_one(t, inv.trace_context_json) for t in polled)
                    )
                    if await self._maybe_finish(investigation_id, manifest):
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("driver tick failed", extra={"investigation_id": investigation_id})
            await self.clock.sleep(self._sleep_seconds())

    async def _poll_one(self, task: TaskRecord, trace_context_json: str | None) -> None:
        self.polls += 1
        outcome = await self.agents.get_task(task.agent_id, task.id, task.attempt)
        result = outcome if isinstance(outcome, TaskResult) else None
        update: TaskStateUpdate = (
            map_task_result(outcome)
            if isinstance(outcome, TaskResult)
            else map_poll_outcome(outcome)
        )
        if update.state is None or update.state is task.status:
            return  # nothing changed: no write, no event, no span (F9)
        if outcome is PollFailure.NOT_FOUND:
            log.warning("agent lost an acknowledged task", extra={"task_id": task.id})
        await self._record(task, update, result, trace_context_json)

    async def _record(
        self,
        task: TaskRecord,
        update: TaskStateUpdate,
        result: TaskResult | None,
        trace_context_json: str | None,
    ) -> None:
        assert update.state is not None
        to = update.state
        fields: dict[str, object] = {}
        if result is not None:
            fields["llm_mode"] = result.llm_mode.value
        if to.is_terminal and result is not None:
            if to is OrchestratorTaskState.COMPLETED:
                fields["finding_ids_json"] = json.dumps(list(result.finding_ids))
            if result.usage is not None:
                fields["usage_json"] = result.usage.model_dump_json()
        if to is OrchestratorTaskState.FAILED and update.reason is not None:
            fields["error_code"] = update.reason.value
            fields["error_category"] = CATEGORY_BY_CODE[update.reason].value
        attrs = {
            "nexus.investigation.id": task.investigation_id,
            "nexus.task.id": task.id,
            "nexus.task.attempt": task.attempt,
            "nexus.task.from": task.status.value,
            "nexus.task.to": to.value,
        }
        parent = propagate.extract(stored_carrier(trace_context_json))
        with self.tracer.start_as_current_span("task.transition", context=parent, attributes=attrs):
            now = self.clock.now()
            async with self.db.transaction() as tx:
                applied = await self.repo.transition_task(
                    tx, task, to, now=now, reason=update.reason, fields=fields
                )
                if applied is not Applied.APPLIED or not to.is_terminal or result is None:
                    return
                usage = result.usage
                if usage is not None:
                    await self.repo.add_tokens_used(
                        tx, task.investigation_id, usage.total_tokens, now
                    )
                    if usage.replayed:
                        await self.repo.append_note(
                            tx,
                            task.investigation_id,
                            TimelineEventType.LLM_REPLAYED,
                            {
                                "task_id": task.id,
                                "agent_id": task.agent_id.value,
                                "model_id": result.model_id,
                            },
                            now,
                        )

    async def _maybe_finish(self, investigation_id: str, manifest: ScenarioManifest | None) -> bool:
        """Apply the terminal rule if every task is terminal. True when the driver can exit."""
        tasks = await self.repo.task_records(investigation_id)
        skipped = skipped_agents(manifest, tasks)
        if terminal.decide(outcomes_in_template_order(manifest, tasks), skipped) is None:
            return False
        async with self.db.transaction() as tx:
            inv = await self.repo.tx_investigation_record(tx, investigation_id)
            if inv is None or inv.status is not InvestigationStatus.RUNNING:
                return True
            fresh = await self.repo.tx_task_records(tx, investigation_id)
            decision = terminal.decide(
                outcomes_in_template_order(manifest, fresh), skipped_agents(manifest, fresh)
            )
            if decision is None:
                return False
            with self.tracer.start_as_current_span(
                "workflow.finish",
                context=propagate.extract(stored_carrier(inv.trace_context_json)),
                attributes={
                    "nexus.investigation.id": investigation_id,
                    "nexus.investigation.status": decision.status.value,
                    "nexus.investigation.reason": decision.reason.value,
                },
            ):
                await self.repo.transition_investigation(
                    tx,
                    investigation_id,
                    decision.status,
                    now=self.clock.now(),
                    reason=decision.reason,
                )
        log.info(
            "investigation finished",
            extra={
                "investigation_id": investigation_id,
                "status": decision.status.value,
                "reason": decision.reason.value,
            },
        )
        return True
