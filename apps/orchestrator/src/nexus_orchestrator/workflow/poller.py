"""Per-investigation driver: dispatch, poll, finish (S2 spec FR-003..FR-007; main spec §8.3, §8.7).

One asyncio task per RUNNING investigation (§8.7). It is stateless: every tick re-reads the
rows, so a restart (resume sweep) just starts a new driver.

Each tick, only while this process **holds the leader lock** (ADR-005 D1):

0. Investigation deadline (FR-123, ``fence.py``): at ``investigations.deadline`` the fence ends it
   (PARTIAL if a specialist COMPLETED, else FAILED, both ``investigation_deadline``), cancelling
   every open task, and the driver exits. Nothing below runs on that tick.
1. Time out every PENDING/DISPATCHED/RUNNING task past its deadline (FR-120, ``deadlines.py``):
   → TIMED_OUT (version guarded), then a best-effort agent cancel for DISPATCHED/RUNNING ones.
2. Re-dispatch (FR-121): a retry-eligible task whose wait window is open and whose agent's
   ``/readyz`` is green goes back to PENDING as attempt 2.
3. Dispatch every PENDING task (any attempt) whose own deadline has not passed (S3 §12: "retried
   each tick until its deadline"). The first dispatch of a ``(task, attempt)`` by this driver runs
   the FR-003 retry loop; every later tick sends **one** try. Dispatches run as background jobs
   (one per task, never two at once), so an agent that is down or hangs cannot hold the tick and
   delay the deadline watcher for the other tasks (NFR-101). The row is re-read before each send;
   the agent's ``(task_id, attempt)`` dedup makes a re-POST safe (ADR-008 F2).
4. Poll every DISPATCHED/RUNNING task through the uninstrumented client and map the result with
   ``status_mapping`` (the only agent↔orchestrator crossing, §8.2). A poll that changes nothing
   writes nothing: no event and no span per poll (F9). An agent ``FAILED(deadline_exceeded)`` at
   attempt 1 is recorded as TIMED_OUT, the same as an orchestrator timeout (FR-121b).
5. When every task row is terminal, apply the S2 terminal rule (``workflow/terminal.py``) and
   exit.

Every agent call in 1-4 (the cancel, the ``/readyz`` probe, the submit, the poll) runs as a
background job, at most one per task (a cancel is tracked apart: the task's re-dispatch waits for
it, since the agent cancels by task id and a late one would stop attempt 2). The steps keep their
order, and their waits share one deadline per tick, ``JOB_WAIT_RATIO`` of the poll interval
(0.5 s at 2 s). A healthy agent answers within it, so a timeout, re-dispatch and attempt-2 POST
still land in one tick; a frozen agent (connected, never answering: the 5 s read timeout is
spec-pinned) costs the other tasks at most that much, and its job carries on alone (NFR-101).

A **resumed** driver (startup sweep, FR-127) runs its first tick as 3, 4 (that poll awaited in
full, not bounded), then 0, 1, 2: what the
agent finished during the outage is kept, and only a task with no terminal answer is timed out.
Past the investigation deadline nothing is re-POSTed, nor is a PENDING task past its own deadline.
After each COMPLETED task the analysis budget is checked and logged only (FR-128, ``budget.py``).

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
from collections.abc import Awaitable, Callable
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
from nexus_orchestrator.workflow import budget, deadlines, terminal
from nexus_orchestrator.workflow.dispatch import Dispatcher, stored_carrier
from nexus_orchestrator.workflow.fence import Fence
from nexus_orchestrator.workflow.transitions import FIRST_ATTEMPT

log = logging.getLogger(__name__)

POLLED_STATES = frozenset({OrchestratorTaskState.DISPATCHED, OrchestratorTaskState.RUNNING})
# A tick waits for its agent-call jobs (dispatch, poll, post-timeout cancel, re-dispatch probe)
# at most this share of the poll interval in total (0.5 s at 2 s). A fast agent answers within
# the tick; a slow or frozen one costs the other tasks at most that much before its job goes on
# alone (NFR-101: tick budget + the longest jittered sleep, 0.5 + 2.4 s, stays ≤ 3 s). The 5 s
# poll read timeout is spec-pinned (§8.3, ADR-003 B7), so the bound is on the wait, not the call.
JOB_WAIT_RATIO = 0.25


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
        # task id -> its one agent-call job (dispatch, poll or re-dispatch), never two at once
        self._inflight: dict[str, asyncio.Task[None]] = {}
        # task id -> the agent cancel sent after its timeout; a re-dispatch waits for it, or a
        # late cancel (by task id) would stop attempt 2 at the agent
        self._cancels: dict[str, asyncio.Task[None]] = {}
        # Test observability: per driver "tick" | "wait" (on its dispatch jobs) | "sleep".
        self._phase: dict[str, str] = {}
        self._waiting_on: dict[str, list[asyncio.Task[None]]] = {}

    # -- lifecycle ------------------------------------------------------------------------------

    def ensure(self, investigation_id: str, *, resumed: bool = False) -> None:
        """Start a driver for this investigation unless one is already running (then a no-op,
        ``resumed`` included). ``resumed`` (the startup sweep, FR-127) only changes the driver's
        first tick: poll before any deadline is applied."""
        if self._stopped:
            return
        current = self._drivers.get(investigation_id)
        if current is not None and not current.done():
            return
        driver = asyncio.create_task(
            self._drive(investigation_id, resumed=resumed), name=f"driver-{investigation_id}"
        )
        self._drivers[investigation_id] = driver

        def forget(_: asyncio.Task[None]) -> None:
            if self._drivers.get(investigation_id) is driver:
                self._phase.pop(investigation_id, None)

        driver.add_done_callback(forget)

    def active(self) -> set[str]:
        return {i for i, t in self._drivers.items() if not t.done()}

    def dispatches_busy(self) -> int:
        """Agent-call jobs that are running and not sleeping between retry tries (for tests)."""
        running = sum(
            1 for j in [*self._inflight.values(), *self._cancels.values()] if not j.done()
        )
        return running - self.dispatcher.backing_off

    def quiescent(self) -> bool:
        """For tests: no driver is mid-tick, none is about to resume from its dispatch wait, and
        no dispatch job is running (one sleeping between retry tries is fine). Pair it with the
        clock's sleeper count: a driver woken by ``advance`` still reads as asleep here."""
        for inv in self.active():
            phase = self._phase.get(inv, "tick")
            if phase == "tick":
                return False
            if phase == "wait" and all(j.done() for j in self._waiting_on.get(inv, [])):
                return False
        return self.dispatches_busy() == 0

    async def stop(self) -> None:
        """Graceful shutdown (ADR-005 D5): cancel drivers, change no state."""
        self._stopped = True
        tasks = [*self._drivers.values(), *self._inflight.values(), *self._cancels.values()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._drivers.clear()
        self._inflight.clear()
        self._cancels.clear()

    def _sleep_seconds(self) -> float:
        base = self.settings.poll_interval_seconds
        jitter = self.settings.poll_jitter_ratio
        return base * random.uniform(1 - jitter, 1 + jitter)

    # -- driver ---------------------------------------------------------------------------------

    async def _drive(self, investigation_id: str, *, resumed: bool = False) -> None:
        manifest: ScenarioManifest | None = None
        first_sent: set[tuple[str, int]] = set()  # (task, attempt) whose first dispatch started
        poll_first = resumed  # FR-127: the first tick polls before it applies any deadline
        while True:
            self._phase[investigation_id] = "tick"
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
                    # (3) re-dispatch (FR-121), then dispatch (every PENDING task with time
                    # left, as background jobs), poll, finish. A resumed driver's
                    # first tick is dispatch, poll, then (1)-(3): what an agent finished while we
                    # were down is kept, and only a task with no terminal answer is timed out.
                    polled_first = False
                    if poll_first:
                        if manifest is not None and self.clock.now() < inv.deadline:
                            # Past the investigation deadline nothing is re-POSTed (FR-123).
                            await self._await_jobs(
                                investigation_id,
                                await self._dispatch_pending(inv, tasks, manifest, first_sent),
                                self.clock.now()
                                + timedelta(
                                    seconds=self.settings.poll_interval_seconds * JOB_WAIT_RATIO
                                ),
                            )
                        await self._poll_open(inv, investigation_id)
                        tasks = await self.repo.task_records(investigation_id)
                        # Cleared only once the poll and the re-read have succeeded: if any of
                        # them raised, the retry is still poll-first (FR-127).
                        poll_first = False
                        polled_first = True
                    if self.clock.now() >= inv.deadline:
                        await self._deadline(inv, tasks)
                        return  # ended, or someone else already ended it: either way we are done
                    # Every agent call below runs as a job. The steps stay in order (a cancel
                    # settles before its task's re-dispatch, attempt 2 is POSTed, then polls),
                    # and all their waits share one deadline: ``JOB_WAIT_RATIO`` of a poll
                    # interval (NFR-101). A healthy agent finishes all of it in this tick; a
                    # frozen one's job carries on alone and its task waits for a later tick.
                    until = self.clock.now() + timedelta(
                        seconds=self.settings.poll_interval_seconds * JOB_WAIT_RATIO
                    )
                    due = deadlines.expired(tasks, self.clock.now())
                    if due:
                        cancels = await asyncio.gather(*(self._time_out(t) for t in due))
                        await self._await_jobs(
                            investigation_id, [c for c in cancels if c is not None], until
                        )
                        tasks = await self.repo.task_records(investigation_id)
                    retry = [t for t in tasks if deadlines.retry_eligible(t)]
                    if retry and manifest is not None:
                        started = self._start_redispatches(retry, inv, manifest)
                        if started:
                            await self._await_jobs(investigation_id, started, until)
                            tasks = await self.repo.task_records(investigation_id)
                    if manifest is not None:
                        await self._await_jobs(
                            investigation_id,
                            await self._dispatch_pending(inv, tasks, manifest, first_sent),
                            until,
                        )
                    if not polled_first:
                        await self._await_jobs(
                            investigation_id, await self._start_polls(inv, investigation_id), until
                        )
                    if await self._maybe_finish(investigation_id, manifest):
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("driver tick failed", extra={"investigation_id": investigation_id})
            finally:
                self._waiting_on.pop(investigation_id, None)
            self._phase[investigation_id] = "sleep"
            await self.clock.sleep(self._sleep_seconds())

    async def _dispatch_pending(
        self,
        inv: InvestigationRecord,
        tasks: list[TaskRecord],
        manifest: ScenarioManifest,
        first_sent: set[tuple[str, int]],
    ) -> list[asyncio.Task[None]]:
        """Start a dispatch job for every PENDING task (any attempt) that still has time left and
        has no job in flight. The first dispatch of a ``(task, attempt)`` runs the FR-003 retry
        loop; later ticks send one try. A task at or past its deadline is left to the deadline
        watcher (FR-120). Returns the jobs started (see ``_await_jobs``)."""
        now = self.clock.now()
        started: list[asyncio.Task[None]] = []
        for t in tasks:
            if t.status is not OrchestratorTaskState.PENDING or now >= t.deadline:
                continue
            if self._busy(t.id):
                continue  # one agent-call job per task at a time; the next tick looks again
            key = (t.id, t.attempt)
            single_shot = key in first_sent
            first_sent.add(key)
            started.append(
                self._start_job(
                    t.id,
                    lambda t=t, one=single_shot: self.dispatcher.dispatch(
                        inv, t, manifest, single_shot=one
                    ),
                    "dispatch",
                )
            )
        return started

    def _start_job(
        self, task_id: str, call: Callable[[], Awaitable[object]], kind: str
    ) -> asyncio.Task[None]:
        """Run one agent call for ``task_id`` off the tick, registered as its in-flight job."""
        job = asyncio.create_task(self._guarded(call, kind, task_id), name=f"{kind}-{task_id}")
        self._inflight[task_id] = job
        job.add_done_callback(lambda done: self._forget_job(task_id, done))
        return job

    def _busy(self, task_id: str) -> bool:
        job = self._inflight.get(task_id)
        return job is not None and not job.done()

    def _start_redispatches(
        self, retry: list[TaskRecord], inv: InvestigationRecord, manifest: ScenarioManifest
    ) -> list[asyncio.Task[None]]:
        """A re-dispatch job (``/readyz`` probe, then the write) per retry-eligible task that has
        no job in flight and whose post-timeout cancel has settled. Its PENDING attempt 2 is
        POSTed by this tick's dispatch step if the job finishes within the tick's wait, else by a
        later tick's."""
        started = []
        for t in retry:
            cancel = self._cancels.get(t.id)
            if self._busy(t.id) or (cancel is not None and not cancel.done()):
                continue
            started.append(
                self._start_job(t.id, lambda t=t: self._redispatch(t, inv, manifest), "redispatch")
            )
        return started

    async def _start_polls(
        self, inv: InvestigationRecord, investigation_id: str
    ) -> list[asyncio.Task[None]]:
        """A poll job per DISPATCHED/RUNNING task with no job in flight (re-read: the dispatch
        step moved some). A frozen agent's poll carries on alone; its task is skipped until the
        poll returns, and a late result is version guarded like any other write."""
        tasks = await self.repo.task_records(investigation_id)
        return [
            self._start_job(t.id, lambda t=t: self._poll_one(t, inv.trace_context_json), "poll")
            for t in tasks
            if t.status in POLLED_STATES and not self._busy(t.id)
        ]

    async def _await_jobs(
        self, investigation_id: str, jobs: list[asyncio.Task[None]], until: datetime
    ) -> None:
        """Wait for this tick's agent-call jobs until ``until`` on the clock (the tick's share of
        ``JOB_WAIT_RATIO``). A job still running after that (a retry loop, a frozen agent)
        carries on by itself, and later ticks skip its task until it is done."""
        if not jobs:
            return
        budget = (until - self.clock.now()).total_seconds()
        if budget <= 0 or all(j.done() for j in jobs):
            await asyncio.sleep(0)  # let jobs that are already done run their callbacks
            return
        timer = asyncio.ensure_future(self.clock.sleep(budget))
        all_done = asyncio.ensure_future(asyncio.wait(jobs))  # cancelling it leaves the jobs
        await asyncio.sleep(0)  # let the timer register on the clock before we say "wait"
        self._waiting_on[investigation_id] = jobs
        self._phase[investigation_id] = "wait"
        try:
            await asyncio.wait({timer, all_done}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._phase[investigation_id] = "tick"
            self._waiting_on.pop(investigation_id, None)
            for waiter in (timer, all_done):
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter

    def _forget_job(self, task_id: str, job: asyncio.Task[None]) -> None:
        if self._inflight.get(task_id) is job:
            del self._inflight[task_id]

    async def _guarded(
        self, call: Callable[[], Awaitable[object]], kind: str, task_id: str
    ) -> None:
        try:
            await call()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(f"{kind} job failed", extra={"task_id": task_id})

    async def _poll_open(self, inv: InvestigationRecord, investigation_id: str) -> None:
        """One poll of every DISPATCHED/RUNNING task (re-read: the dispatch step moved some)."""
        tasks = await self.repo.task_records(investigation_id)
        polled = [t for t in tasks if t.status in POLLED_STATES]
        await asyncio.gather(*(self._poll_one(t, inv.trace_context_json) for t in polled))

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
        applied = await self._record(task, update, result, trace_context_json)
        if applied is Applied.APPLIED and update.state is OrchestratorTaskState.COMPLETED:
            await self._check_budget(task.investigation_id)

    async def _check_budget(self, investigation_id: str) -> None:
        """FR-128: after a COMPLETED task, is what is left under the analysis minimum? Analysis is
        not enabled in S3, so this is only logged; S4 turns it into ``PARTIAL(budget_exceeded)``."""
        cap = self.settings.nexus_max_tokens_per_investigation
        record = await self.repo.investigation_record(investigation_id)
        if record is None:
            return
        used = record.tokens_used  # the cumulative ledger: every attempt's spend, not the last's
        if budget.analysis_budget_short(cap, used, self.settings.min_analysis_tokens):
            log.info(
                "analysis budget short",
                extra={
                    "investigation_id": investigation_id,
                    "cap": cap,
                    "used": used,
                    "remaining": cap - used,
                    "min": self.settings.min_analysis_tokens,
                },
            )

    async def _time_out(self, task: TaskRecord) -> asyncio.Task[None] | None:
        """FR-120: ``task`` is past its deadline → TIMED_OUT(deadline_exceeded), version guarded.

        The agent is told to stop only **after** the commit and only if this write was the one
        that applied: a task that finished first (DROPPED/NOOP) is not cancelled. The cancel is
        best effort (FR-102) and never fails the tick: it runs as a job (created inside the
        ``workflow.timeout`` span, so its HTTP span stays a child), returned for the tick's bounded
        wait. A re-dispatch of this task waits until it has settled."""
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
            if applied is not Applied.APPLIED or task.status not in POLLED_STATES:
                return None
            job = asyncio.create_task(
                self._guarded(
                    lambda: self.agents.cancel(task.agent_id, task.id), "cancel", task.id
                ),
                name=f"cancel-{task.id}",
            )
        self._cancels[task.id] = job
        job.add_done_callback(
            lambda done: (
                self._cancels.pop(task.id, None) if self._cancels.get(task.id) is done else None
            )
        )
        return job

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
        dispatch step then POSTs it (this runs as a job: see ``_start_redispatches``). Otherwise
        nothing is written; the next tick asks again until
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
                # A poll result or timeout is about ``task.attempt``: once a later attempt owns
                # the row (a poll job that outlived its leader, say) it must not move it (FR-122).
                row = await tx.fetchone("SELECT attempt FROM tasks WHERE id = ?", (task.id,))
                if row is None or row["attempt"] != task.attempt:
                    log.warning(
                        "write for an attempt that is over; ignored",
                        extra={"task_id": task.id, "attempt": task.attempt, "to": to.value},
                    )
                    return Applied.DROPPED
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
