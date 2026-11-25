"""Frontend investigation API (spec §6.1, §8.7, §12.1; ADR-006 E4).

- ``POST /api/v1/investigations`` → ``InvestigationCreateRequest``. ``objective`` defaults to the
  scenario template's; in replay mode any other value gets ``422 replay_objective_locked``
  (§13.2, enforced by the orchestrator, which knows the mode).
- ``GET /api/v1/investigations/{id}`` → ``Investigation`` (with its ``TaskView`` list).
- ``GET /api/v1/investigations`` → ``InvestigationPage`` (cursor pagination, §12).

Read-model fields mirror the ``investigations``/``tasks`` tables (§8.7) minus internals
(``version``, ``context_closed``, ``trace_context_json``, ``token_reservation``, ``acked_at``).
"""

from __future__ import annotations

from pydantic import Field

from nexus_contracts.a2a.task import TaskType, TaskUsage
from nexus_contracts.api.errors import ErrorCategory, ErrorCode
from nexus_contracts.api.states import InvestigationStatus, OrchestratorTaskState
from nexus_contracts.common import (
    AgentId,
    AssessmentId,
    Attempt,
    FindingId,
    InvestigationId,
    ManifestId,
    ShortText,
    TaskId,
    UtcDatetime,
    ValueModel,
    WireModel,
)


class InvestigationCreateRequest(WireModel):
    scenario_id: ManifestId
    objective: ShortText | None = None


class TaskView(ValueModel):
    id: TaskId
    agent_id: AgentId
    task_type: TaskType
    status: OrchestratorTaskState
    status_reason: ErrorCode | None = None
    attempt: Attempt
    deadline: UtcDatetime
    finding_ids: list[FindingId] = Field(default_factory=list)
    usage: TaskUsage | None = None
    error_category: ErrorCategory | None = None
    error_code: ErrorCode | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class Investigation(ValueModel):
    id: InvestigationId
    scenario_id: ManifestId
    objective: ShortText
    status: InvestigationStatus
    status_reason: ErrorCode | None = None
    tokens_used: int = Field(ge=0)
    token_budget: int = Field(gt=0)
    deadline: UtcDatetime
    assessment_id: AssessmentId | None = None
    tasks: list[TaskView] = Field(default_factory=list)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class InvestigationPage(ValueModel):
    items: list[Investigation]
    next_cursor: str | None = None
