"""The single crossing point between agent status and orchestrator task state (spec §8.2).

Plain version: the agent speaks A2A (``CANCELED``, ``SUBMITTED`` …); the orchestrator keeps its
own richer states (``CANCELLED``, ``DISPATCHED``, ``TIMED_OUT`` …). This function is the
translation table, and nothing else may translate between them (§8.2 "Spelling", ADR-004 C2).

Table (§8.2):

====================================  ==========================================
Agent status / poll outcome           Orchestrator task state
====================================  ==========================================
SUBMITTED                             DISPATCHED
WORKING                               RUNNING
COMPLETED                             COMPLETED
FAILED (agent_restarted)              FAILED (agent_restarted), re-dispatchable
FAILED (other code)                   FAILED (code), not re-dispatched
REJECTED                              FAILED (task_rejected), not re-dispatched
CANCELED                              CANCELLED
404                                   FAILED (task_lost), re-dispatchable
connection error / 5xx / timeout      no change; keep polling until the deadline
====================================  ==========================================

Stickiness of terminal states and the attempt limit are workflow rules (apps/orchestrator),
not part of this mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nexus_contracts.a2a.task import AgentTaskStatus, TaskResult
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import OrchestratorTaskState

REDISPATCHABLE_CODES: frozenset[ErrorCode] = frozenset(
    {ErrorCode.AGENT_RESTARTED, ErrorCode.TASK_LOST}
)
"""``FAILED`` reasons that allow the single re-dispatch (§8.2, §8.5). ``TIMED_OUT`` is also
re-dispatchable but is set by the orchestrator, never by a poll."""


class PollFailure(StrEnum):
    """Poll outcomes that carry no agent status (§8.3, ADR-003 B2/B7)."""

    NOT_FOUND = "not_found"  # HTTP 404
    TRANSIENT = "transient"  # connection error, timeout or 5xx


@dataclass(frozen=True, slots=True)
class TaskStateUpdate:
    """Result of mapping one poll. ``state is None`` means "no change, keep polling"."""

    state: OrchestratorTaskState | None
    reason: ErrorCode | None = None
    redispatchable: bool = False


_DIRECT: dict[AgentTaskStatus, OrchestratorTaskState] = {
    AgentTaskStatus.SUBMITTED: OrchestratorTaskState.DISPATCHED,
    AgentTaskStatus.WORKING: OrchestratorTaskState.RUNNING,
    AgentTaskStatus.COMPLETED: OrchestratorTaskState.COMPLETED,
    AgentTaskStatus.CANCELED: OrchestratorTaskState.CANCELLED,
}


def map_poll_outcome(
    outcome: AgentTaskStatus | PollFailure, error_code: ErrorCode | None = None
) -> TaskStateUpdate:
    """Map an agent status (or a status-less poll failure) to an orchestrator update.

    ``error_code`` is required for ``FAILED`` and ignored otherwise.
    """
    if outcome is PollFailure.TRANSIENT:
        return TaskStateUpdate(state=None)
    if outcome is PollFailure.NOT_FOUND:
        return TaskStateUpdate(OrchestratorTaskState.FAILED, ErrorCode.TASK_LOST, True)
    if outcome is AgentTaskStatus.REJECTED:
        return TaskStateUpdate(OrchestratorTaskState.FAILED, ErrorCode.TASK_REJECTED, False)
    if outcome is AgentTaskStatus.FAILED:
        if error_code is None:
            raise ValueError("FAILED needs an error code to map (§8.2)")
        return TaskStateUpdate(
            OrchestratorTaskState.FAILED, error_code, error_code in REDISPATCHABLE_CODES
        )
    return TaskStateUpdate(_DIRECT[AgentTaskStatus(outcome)])


def map_task_result(result: TaskResult) -> TaskStateUpdate:
    """Convenience wrapper for a successful ``GET /v1/tasks/{id}`` body."""
    return map_poll_outcome(result.status, result.error.code if result.error else None)
