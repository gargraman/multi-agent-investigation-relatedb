"""Task deadlines and the re-dispatch wait window (S3b FR-120..FR-122; main spec §8.5).

Plain version: every tick the driver asks "which open tasks are past their deadline?" and the
poller times those out. Then it asks "which failed tasks may still get a second attempt?". This
module is only those questions; the writes (``TIMED_OUT``, the move back to ``PENDING``) and the
side effects (agent cancel, readiness check) live in ``Poller``.

The wait window (FR-121) starts the moment a task became retry-eligible (``tasks.updated_at``)
and ends at ``min(start + task_deadline_seconds, investigation deadline)``. No column is added.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Sequence
from datetime import datetime, timedelta

from nexus_contracts.a2a.status_mapping import REDISPATCHABLE_CODES
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import OrchestratorTaskState
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.repository import TaskRecord
from nexus_orchestrator.workflow.transitions import FIRST_ATTEMPT

DEADLINE_STATES = frozenset(
    {
        OrchestratorTaskState.PENDING,
        OrchestratorTaskState.DISPATCHED,
        OrchestratorTaskState.RUNNING,
    }
)


def expired(tasks: Sequence[TaskRecord], now: datetime) -> list[TaskRecord]:
    """PENDING/DISPATCHED/RUNNING tasks with ``now >= deadline``, in input order."""
    return [t for t in tasks if t.status in DEADLINE_STATES and now >= t.deadline]


def retry_eligible(task: TaskRecord) -> bool:
    """FR-121: attempt 1 that is TIMED_OUT, or FAILED with a re-dispatchable reason. Attempt 2 is
    never eligible (FR-122)."""
    if task.attempt != FIRST_ATTEMPT:
        return False
    if task.status is OrchestratorTaskState.TIMED_OUT:
        return True
    if task.status is not OrchestratorTaskState.FAILED or task.status_reason is None:
        return False
    with contextlib.suppress(ValueError):
        return ErrorCode(task.status_reason) in REDISPATCHABLE_CODES
    return False


def attempt2_reservation(share: float, cap: int, usage_tokens: int | None) -> int:
    """FR-121: ``floor(share x cap)`` minus what attempt 1 spent (unknown spend counts as 0).
    Zero or negative means there is nothing left to retry with."""
    return math.floor(share * cap) - (usage_tokens or 0)


def retry_window_end(
    task: TaskRecord, investigation_deadline: datetime, settings: OrchestratorSettings
) -> datetime:
    """FR-121 wait window: ``min(updated_at + task deadline, investigation deadline)``."""
    return min(
        task.updated_at + timedelta(seconds=settings.task_deadline_seconds), investigation_deadline
    )


def retry_window_open(
    task: TaskRecord,
    investigation_deadline: datetime,
    now: datetime,
    settings: OrchestratorSettings,
) -> bool:
    """FR-121a: the investigation must treat this task as not finished yet.

    Eligible, before the window end, and with a reservation left. The stored attempt-1
    reservation is ``floor(share x cap)`` (creation), so ``reservation - usage`` is
    ``attempt2_reservation``. An exhausted reservation closes the window at once, so the
    investigation is not held open for a retry that will never be made."""
    return (
        retry_eligible(task)
        and now < retry_window_end(task, investigation_deadline, settings)
        and task.token_reservation - (task.usage_tokens or 0) > 0
    )
