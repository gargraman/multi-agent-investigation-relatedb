"""Timeline / SSE event envelope (spec §6.2, §8.1 step 16, §8.2, §8.3, §8.7, §12.1, §13.2).

One row of ``timeline_events(seq, investigation_id, type, payload_json, created_at)``. Over SSE,
``seq`` is the event ``id`` (``Last-Event-ID``), ``type`` the event name and the envelope JSON the
``data``. Events are published only after their SQLite commit.

The spec names *when* events are written but defines no type list. ``TimelineEventType`` is
derived from those sentences; status-change payloads are typed, the rest are free-form dicts.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus, OrchestratorTaskState
from nexus_contracts.common import (
    AgentId,
    Attempt,
    InvestigationId,
    TaskId,
    UtcDatetime,
    ValueModel,
)


class TimelineEventType(StrEnum):
    INVESTIGATION_STATUS_CHANGED = "investigation.status_changed"  # §8.1 step 16
    TASK_STATUS_CHANGED = "task.status_changed"  # §8.1 step 16
    TASK_LATE_RESULT = "task.late_result"  # §8.2 sticky terminal states
    TRANSITION_DROPPED = "transition.dropped"  # §8.3 / ADR-008 F4 version conflict note
    LLM_REPLAYED = "llm.replayed"  # §13.2 "the timeline marks every replayed call"


class InvestigationStatusChanged(ValueModel):
    from_status: InvestigationStatus | None = None
    to_status: InvestigationStatus
    reason: ErrorCode | None = None


class TaskStatusChanged(ValueModel):
    task_id: TaskId
    agent_id: AgentId
    attempt: Attempt
    from_state: OrchestratorTaskState | None = None
    to_state: OrchestratorTaskState
    reason: ErrorCode | None = None


class TaskLateResult(ValueModel):
    task_id: TaskId
    attempt: Attempt
    agent_status: AgentTaskStatus
    recorded_state: OrchestratorTaskState


_TYPED_PAYLOADS: dict[TimelineEventType, type[ValueModel]] = {
    TimelineEventType.INVESTIGATION_STATUS_CHANGED: InvestigationStatusChanged,
    TimelineEventType.TASK_STATUS_CHANGED: TaskStatusChanged,
    TimelineEventType.TASK_LATE_RESULT: TaskLateResult,
}


class TimelineEvent(ValueModel):
    """Envelope. For typed event types the payload must validate against its model."""

    seq: int = Field(ge=1)
    investigation_id: InvestigationId
    type: TimelineEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _payload_shape(self) -> TimelineEvent:
        model = _TYPED_PAYLOADS.get(self.type)
        if model is not None:
            model.model_validate(self.payload)
        return self

    def typed_payload(self) -> ValueModel | dict[str, Any]:
        model = _TYPED_PAYLOADS.get(self.type)
        return model.model_validate(self.payload) if model else self.payload


class TimelinePage(ValueModel):
    """``GET /api/v1/investigations/{id}/timeline`` (cursor, §12.1)."""

    items: list[TimelineEvent]
    next_cursor: str | None = None
