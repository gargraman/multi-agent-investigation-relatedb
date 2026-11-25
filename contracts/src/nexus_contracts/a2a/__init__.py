"""A2A-inspired agent task contracts (spec §12.2, ADR-004)."""

from nexus_contracts.a2a.status_mapping import (
    REDISPATCHABLE_CODES,
    PollFailure,
    TaskStateUpdate,
    map_poll_outcome,
    map_task_result,
)
from nexus_contracts.a2a.task import (
    AgentCard,
    AgentTaskStatus,
    TaskError,
    TaskRequest,
    TaskResult,
    TaskSubmitResponse,
    TaskUsage,
    TraceContext,
)

__all__ = [
    "REDISPATCHABLE_CODES",
    "AgentCard",
    "AgentTaskStatus",
    "PollFailure",
    "TaskError",
    "TaskRequest",
    "TaskResult",
    "TaskStateUpdate",
    "TaskSubmitResponse",
    "TaskUsage",
    "TraceContext",
    "map_poll_outcome",
    "map_task_result",
]
