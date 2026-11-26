"""Per-investigation driver: dispatch, poll, finish (S2 spec FR-003..FR-007; main spec §8.3, §8.7).

One asyncio task per RUNNING investigation (§8.7). It is stateless: every tick re-reads the
rows, so a restart (resume sweep) just starts a new driver.

Each tick, only while this process **holds the leader lock** (ADR-005 D1):

0. Investigation deadline (FR-123, ``fence.py``): at ``investigations.deadline`` the fence ends it
   (PARTIAL if a specialist COMPLETED, else FAILED, both ``investigation_deadline``), cancelling
   every open task, and the driver exits. Nothing below runs on that tick.
1. Time out every PENDING/DISPATCHED/RUNNING task past its deadline (FR-120, ``deadlines.py``):
   → TIMED_OUT (version guarded), then a best-effort agent cancel for DISPATCHED/RUNNING ones.
2. On the driver's first leader tick, dispatch every PENDING task (same ``attempt``; the agent's
   ``(task_id, attempt)`` dedup makes a resume re-POST safe, ADR-008 F2).
3. Poll every DISPATCHED/RUNNING task through the uninstrumented client and map the result with
   ``status_mapping`` (the only agent↔orchestrator crossing, §8.2). A poll that changes nothing
   writes nothing: no event and no span per poll (F9). An agent ``FAILED(deadline_exceeded)`` at
   attempt 1 is recorded as TIMED_OUT, the same as an orchestrator timeout (FR-121b).
4. When every task row is terminal, apply the S2 terminal rule (``workflow/terminal.py``) and
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
from datetime import datetime, timedelta

from opentelemetry import propagate
from opentelemetry.trace import Tracer

from nexus_contracts.a2a.status_mapping import (
    REDISPATCHABLE_CODES,
    PollFailure,
    TaskStateUpdate,
    map_poll_outcome,
    map_task_result,
)
from nexus_contracts.a2a.task import TaskResult
from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCode
from nexus_contracts.api.states import InvestigationStatus, OrchestratorTaskState
from nexus_contracts.common import AgentId
from nexus_contracts.events.timeline import TimelineEventType
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_orchestrator.clock import Clock, SystemClock
from nexus_orchestrator.delegation.agent_client import AgentClient
from nexus_orchestrator.scenarios import ScenarioCatalog
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.repository import (
    Applied,
    InvestigationRecord,
    Repository,
    TaskRecord,
)
from nexus_orchestrator.workflow import deadlines, terminal
from nexus_orchestrator.workflow.dispatch import Dispatcher, stored_carrier
from nexus_orchestrator.workflow.fence import Fence
from nexus_orchestrator.workflow.transitions import FIRST_ATTEMPT

log = logging.getLogger(__name__)

POLLED_STATES = frozenset({OrchestratorTaskState.DISPATCHED, OrchestratorTaskState.RUNNING})


def skipped_agents(manifest: ScenarioManifest | None, tasks: list[TaskRecord]) -> list[AgentId]:
    """Template agents without a task row: the ones not enabled when the tasks were created."""
    if manifest is None:
        return []
    have = {t.agent_id for t in tasks}
    return [t.agent for t in manifest.template.tasks if t.agent not in have]


def outcomes_in_template_order(
    manifest: ScenarioManifest | None,
    tasks: list[TaskRecord],
    investigation_deadline: datetime,
    now: datetime,
    settings: OrchestratorSettings,
) -> list[terminal.TaskOutcome]:
    """Task outcomes ordered by their agent's manifest-template index (FR-124); a task whose
    agent is not in the template goes last. A task whose re-dispatch window is still open is
    ``retry_open``, so the investigation is not finished before attempt 2 can start (FR-121a)."""
    order = [t.agent for t in manifest.template.tasks] if manifest is not None else []
    rank = {agent: i for i, agent in enumerate(order)}
    ordered = sorted(tasks, key=lambda t: rank.get(t.agent_id, len(order)))  # stable
    return [
        terminal.TaskOutcome(
            t.agent_id,
            t.status,
            t.status_reason,
            deadlines.retry_window_open(t, investigation_deadline, now, settings),
        )
        for t in ordered
    ]


class Poller:
    def __init__(
        self,
        settings: OrchestratorSettings,
        db: Database,
        repo: Repository,
        catalog: ScenarioCatalog,
        agents: AgentClient,
        dispatcher: Dispatcher,
        fence: Fence,
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
        self.fence = fence
        self.tracer = tracer
        self.is_leader = is_leader
        self._drivers: dict[str, asyncio.Task[None]] = {}
        self._no_reservation_logged: set[str] = set()  # log "nothing to retry with" once per task
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

    async def stop_one(self, investigation_id: str) -> None:
        """Cancel and await one investigation's driver (cancel endpoint, FR-125). A no-op when
        there is none, and when called from inside that driver's own task (it is already on its
        way out, and awaiting itself would never return)."""
        task = self._drivers.get(investigation_id)
        if task is None or task is asyncio.current_task():
            return
        del self._drivers[investigation_id]
        task.cancel()
        # Unlike ``await task`` this never re-raises the driver's outcome, and it is bounded: a
        # driver stuck in a step that ignores cancellation must not hold the cancel response.
        _, pending = await asyncio.wait({task}, timeout=self.settings.cancel_step_timeout_seconds)
        if pending:
            log.warning(
                "driver did not stop in time after cancel",
                extra={"investigation_id": investigation_id},
            )

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
                    # Tick order: (1) investigation deadline (FR-123), (2) task deadlines,
                    # (3) re-dispatch (FR-121), then dispatch, poll, finish.
                    if self.clock.now() >= inv.deadline:
                        await self._deadline(inv, tasks)
                        return  # ended, or someone else already ended it: either way we are done
                    due = deadlines.expired(tasks, self.clock.now())
                    if due:
                        await asyncio.gather(*(self._time_out(t) for t in due))
                        tasks = await self.repo.task_records(investigation_id)
                    retry = [t for t in tasks if deadlines.retry_eligible(t)]
                    if retry and manifest is not None:
                        await asyncio.gather(*(self._redispatch(t, inv, manifest) for t in retry))
                        tasks = await self.repo.task_records(investigation_id)
                    if manifest is not None:
                        # First leader tick: every PENDING task (a resume re-POSTs, F2). After
                        # that only a re-dispatched one (attempt > 1): a first-attempt task the
                        # agent could not be reached for stays PENDING until its deadline (G13).
                        pending = [
                            t
                            for t in tasks
                            if t.status is OrchestratorTaskState.PENDING
                            and (not dispatched or t.attempt > FIRST_ATTEMPT)
                        ]
                        await asyncio.gather(
                            *(self.dispatcher.dispatch(inv, t, manifest) for t in pending)
                        )
                        dispatched = dispatched or self.is_leader()  # lost it mid-way: retry
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

    async def _deadline(self, inv: InvestigationRecord, tasks: list[TaskRecord]) -> None:
        """FR-123: the investigation is out of time. PARTIAL if a specialist COMPLETED, else
        FAILED; the fence cancels the open tasks (and closes the Context Service first)."""
        completed = any(
            t.agent_id in terminal.SPECIALISTS and t.status is OrchestratorTaskState.COMPLETED
            for t in tasks
        )
        status = InvestigationStatus.PARTIAL if completed else InvestigationStatus.FAILED
        reason = ErrorCode.INVESTIGATION_DEADLINE.value
        attrs = {
            "nexus.investigation.id": inv.id,
            "nexus.investigation.status": status.value,
            "nexus.investigation.reason": reason,
        }
        parent = propagate.extract(stored_carrier(inv.trace_context_json))
        with self.tracer.start_as_current_span(
            "workflow.deadline", context=parent, attributes=attrs
        ):
            result = await self.fence.end(inv.id, status, reason, task_reason=reason)
        log.info(
            "investigation deadline",
            extra={
                "investigation_id": inv.id,
                "status": result.status.value,
                "applied": result.applied,
                "context_closed": result.context_closed,
            },
        )

    async def _poll_one(self, task: TaskRecord, trace_context_json: str | None) -> None:
        self.polls += 1
        outcome = await self.agents.get_task(task.agent_id, task.id, task.attempt)
        result = outcome if isinstance(outcome, TaskResult) else None
        if result is not None and result.attempt != task.attempt:
            # A late reply from an earlier attempt must not move the current one (FR-122).
            log.debug(
                "ignoring a result for another attempt",
                extra={
                    "task_id": task.id,
                    "attempt": task.attempt,
                    "result_attempt": result.attempt,
                },
            )
            return
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

    async def _time_out(self, task: TaskRecord) -> None:
        """FR-120: ``task`` is past its deadline → TIMED_OUT(deadline_exceeded), version guarded.

        The agent is told to stop only **after** the commit and only if this write was the one
        that applied: a task that finished first (DROPPED/NOOP) is not cancelled. The cancel is
        best effort (FR-102) and never fails the tick."""
        trace_context_json = await self.repo.get_trace_context(task.investigation_id)
        attrs = {
            "nexus.investigation.id": task.investigation_id,
            "nexus.task.id": task.id,
            "nexus.task.attempt": task.attempt,
            "nexus.agent.id": task.agent_id.value,
            "nexus.task.from": task.status.value,
        }
        parent = propagate.extract(stored_carrier(trace_context_json))
        with self.tracer.start_as_current_span(
            "workflow.timeout", context=parent, attributes=attrs
        ):
            update = TaskStateUpdate(OrchestratorTaskState.TIMED_OUT, ErrorCode.DEADLINE_EXCEEDED)
            applied = await self._record(task, update, None, trace_context_json)
            if applied is Applied.APPLIED and task.status in POLLED_STATES:
                try:
                    await self.agents.cancel(task.agent_id, task.id)
                except Exception:
                    log.warning("agent cancel failed", extra={"task_id": task.id}, exc_info=True)

    def _note_no_reservation(self, task: TaskRecord, reservation: int) -> None:
        """Attempt 1 used the whole share: there is nothing to retry with, so the task stays
        final. Said once per task (the tick that failed it, or a later one), not once per tick."""
        if task.id in self._no_reservation_logged:
            return
        self._no_reservation_logged.add(task.id)
        log.warning(
            "no token reservation left for attempt 2; task stays final",
            extra={
                "task_id": task.id,
                "investigation_id": task.investigation_id,
                "reservation": reservation,
            },
        )

    async def _redispatch(
        self, task: TaskRecord, inv: InvestigationRecord, manifest: ScenarioManifest
    ) -> None:
        """FR-121: a retry-eligible ``task`` goes back to PENDING as attempt 2, if its wait window
        is open, it has a reservation left and the agent's ``/readyz`` is green. The same tick's
        dispatch step then POSTs it. Otherwise nothing is written; the next tick asks again until
        the window closes, after which the task is final.

        The readiness await is outside any transaction. Afterwards the task and the investigation
        are re-read, so a write that landed meanwhile (an investigation cancel, a terminal write)
        wins and the re-dispatch is dropped."""
        if self.clock.now() >= deadlines.retry_window_end(task, inv.deadline, self.settings):
            return
        template = next((t for t in manifest.template.tasks if t.agent is task.agent_id), None)
        if template is None:
            return
        reservation = deadlines.attempt2_reservation(
            template.token_share,
            self.settings.nexus_max_tokens_per_investigation,
            task.usage_tokens,
        )
        if reservation <= 0:
            self._note_no_reservation(task, reservation)
            return
        attrs = {
            "nexus.investigation.id": task.investigation_id,
            "nexus.task.id": task.id,
            "nexus.task.attempt": task.attempt,
            "nexus.agent.id": task.agent_id.value,
        }
        parent = propagate.extract(stored_carrier(inv.trace_context_json))
        with self.tracer.start_as_current_span(
            "workflow.redispatch", context=parent, attributes=attrs
        ) as span:
            if not await self.agents.ready(task.agent_id):
                span.set_attribute("nexus.redispatch.outcome", "not_ready")
                return
            now = self.clock.now()
            async with self.db.transaction() as tx:
                fresh_inv = await self.repo.tx_investigation_record(tx, inv.id)
                fresh = next(
                    (t for t in await self.repo.tx_task_records(tx, inv.id) if t.id == task.id),
                    None,
                )
                if (
                    fresh_inv is None
                    or fresh_inv.status is not InvestigationStatus.RUNNING
                    or fresh is None
                    or fresh.attempt != task.attempt
                    or now >= deadlines.retry_window_end(fresh, inv.deadline, self.settings)
                ):
                    span.set_attribute("nexus.redispatch.outcome", "dropped")
                    return
                attempt = fresh.attempt + 1
                applied = await self.repo.transition_task(
                    tx,
                    fresh,
                    OrchestratorTaskState.PENDING,
                    now=now,
                    fields={
                        "attempt": attempt,
                        "deadline": min(
                            now + timedelta(seconds=self.settings.task_deadline_seconds),
                            inv.deadline,
                        ),
                        "token_reservation": reservation,
                        # attempt 1's outcome must not linger on the attempt-2 row
                        "error_code": None,
                        "error_category": None,
                        "acked_at": None,
                    },
                )
            span.set_attribute("nexus.redispatch.outcome", applied.value)
        if applied is Applied.APPLIED:
            log.info(
                "task re-dispatched",
                extra={
                    "task_id": task.id,
                    "investigation_id": task.investigation_id,
                    "attempt": attempt,
                    "reservation": reservation,
                },
            )

    async def _record(
        self,
        task: TaskRecord,
        update: TaskStateUpdate,
        result: TaskResult | None,
        trace_context_json: str | None,
    ) -> Applied:
        assert update.state is not None
        to = update.state
        if (
            to is OrchestratorTaskState.FAILED
            and update.reason is ErrorCode.DEADLINE_EXCEEDED
            and task.attempt == FIRST_ATTEMPT
        ):
            to = OrchestratorTaskState.TIMED_OUT  # FR-121b: an agent-reported timeout is a timeout
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
                if applied is Applied.APPLIED and self._retry_candidate(task, to, update.reason):
                    spent = result.usage.total_tokens if result and result.usage else 0
                    if task.token_reservation - spent <= 0:  # same figure as the window check
                        self._note_no_reservation(task, task.token_reservation - spent)
                if applied is not Applied.APPLIED or not to.is_terminal or result is None:
                    return applied
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
        return applied

    @staticmethod
    def _retry_candidate(
        task: TaskRecord, to: OrchestratorTaskState, reason: ErrorCode | None
    ) -> bool:
        """Would ``task`` be retry-eligible (FR-121) once it moves to ``to`` with ``reason``?"""
        if task.attempt != FIRST_ATTEMPT:
            return False
        return to is OrchestratorTaskState.TIMED_OUT or (
            to is OrchestratorTaskState.FAILED and reason in REDISPATCHABLE_CODES
        )

    async def _maybe_finish(self, investigation_id: str, manifest: ScenarioManifest | None) -> bool:
        """Apply the terminal rule if every task is terminal. True when the driver can exit."""
        record = await self.repo.investigation_record(investigation_id)
        if record is None:
            return True
        tasks = await self.repo.task_records(investigation_id)
        outcomes = outcomes_in_template_order(
            manifest, tasks, record.deadline, self.clock.now(), self.settings
        )
        if terminal.decide(outcomes, skipped_agents(manifest, tasks)) is None:
            return False
        async with self.db.transaction() as tx:
            inv = await self.repo.tx_investigation_record(tx, investigation_id)
            if inv is None or inv.status is not InvestigationStatus.RUNNING:
                return True
            fresh = await self.repo.tx_task_records(tx, investigation_id)
            decision = terminal.decide(
                outcomes_in_template_order(
                    manifest, fresh, inv.deadline, self.clock.now(), self.settings
                ),
                skipped_agents(manifest, fresh),
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
