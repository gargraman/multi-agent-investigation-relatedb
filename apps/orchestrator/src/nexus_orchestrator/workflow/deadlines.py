"""Task deadlines (S3b FR-120; main spec §8.5).

Plain version: every tick the driver asks "which open tasks are past their deadline?" and the
poller times those out. This module is only that question; the write (``TIMED_OUT``, version
guarded) and the best-effort agent cancel live in ``Poller._time_out``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from nexus_contracts.api.states import OrchestratorTaskState
from nexus_orchestrator.state.repository import TaskRecord

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
