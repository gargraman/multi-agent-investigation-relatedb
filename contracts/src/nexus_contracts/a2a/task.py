"""Agent task API bodies (spec §12.2, §8.2, §8.3; ADR-004; ADR-006 E3/E7).

Endpoints: ``GET /.well-known/agent-card.json`` -> ``AgentCard``; ``POST /v1/tasks`` with
``TaskRequest`` -> ``202 TaskSubmitResponse``; ``GET /v1/tasks/{id}`` -> ``TaskResult`` (status
plus result in one body); ``POST /v1/tasks/{id}/cancel`` (the spec defines no response body).

``task_type`` is deliberately a free string, not an enum: an unknown task type must reach the
agent so it can answer ``REJECTED`` (§8.2, ADR-004 C2), not die as a ``422``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCategory, ErrorCode
from nexus_contracts.common import (
    AgentId,
    AssessmentId,
    Attempt,
    Classification,
    FindingId,
    InvestigationId,
    LlmMode,
    ManifestId,
    ShortText,
    TaskId,
    UtcDatetime,
    ValueModel,
)

TaskType = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class AgentTaskStatus(StrEnum):
    """Agent task status on the wire, per attempt. Names match A2A v1.0.0 ``TaskState``
    (§8.2, ADR-004 C2). Note the A2A spelling ``CANCELED``."""

    SUBMITTED = "SUBMITTED"
    WORKING = "WORKING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self not in {AgentTaskStatus.SUBMITTED, AgentTaskStatus.WORKING}


class TraceContext(ValueModel):
    """W3C trace context propagated in the task body (§8.7, ADR-003 consequences)."""

    traceparent: str = Field(min_length=1, max_length=256)
    tracestate: str = Field(default="", max_length=512)


class TaskRequest(ValueModel):
    """``POST /v1/tasks`` body (§12.2). The agent dedups on ``(task_id, attempt)`` (§8.3)."""

    task_id: TaskId
    investigation_id: InvestigationId
    task_type: TaskType
    objective: ShortText
    input_refs: list[ManifestId] = Field(description="Evidence manifest ids (§11.3 step 2).")
    scenario_id: ManifestId
    classification_ceiling: Classification
    attempt: Attempt
    token_budget: int = Field(gt=0, description="Tokens this attempt may spend (§8.5).")
    deadline: UtcDatetime = Field(description="Absolute; agent self-cancels at +5 s (§8.3).")
    contract_version: str = Field(pattern=r"^\d+\.\d+$")
    trace_context: TraceContext

    @model_validator(mode="after")
    def _unique_refs(self) -> TaskRequest:
        if len(set(self.input_refs)) != len(self.input_refs):
            raise ValueError("input_refs must not contain duplicates")
        return self


class TaskSubmitResponse(ValueModel):
    """``202`` body of ``POST /v1/tasks``: ``{task_id, status}`` (§12.2)."""

    task_id: TaskId
    status: Literal[AgentTaskStatus.SUBMITTED, AgentTaskStatus.REJECTED]


class TaskUsage(ValueModel):
    """Token usage of one attempt (§8.5, §12.2). ``replayed`` marks recorded usage served in
    replay mode (§13.2, ADR-006 E7); it defaults to ``false`` because the §12.2 example omits it."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    llm_calls: int = Field(ge=0)
    replayed: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class TaskError(ValueModel):
    """``error`` = ``{category, code}``; never raw content (§12.2, §8.7)."""

    category: ErrorCategory
    code: ErrorCode

    @classmethod
    def for_code(cls, code: ErrorCode) -> TaskError:
        return cls(category=CATEGORY_BY_CODE[code], code=code)

    @model_validator(mode="after")
    def _category_matches(self) -> TaskError:
        expected = CATEGORY_BY_CODE[self.code]
        if self.category != expected:
            raise ValueError(f"code {self.code} belongs to category {expected}")
        return self


class TaskResult(ValueModel):
    """``GET /v1/tasks/{id}`` body: status plus result (§12.2).

    Local rules: ``FAILED`` requires ``error``; only ``FAILED``/``REJECTED`` may carry one;
    ``finding_ids``/``assessment_id`` only on ``COMPLETED``; ``completed_at`` only on a terminal
    status. ``assessment_id`` is an addition: §17.1 requires Analysis to report the committed
    ``assessment_id`` but the §12.2 result has no field for it.
    """

    task_id: TaskId
    agent_id: AgentId
    status: AgentTaskStatus
    attempt: Attempt
    llm_mode: LlmMode
    summary: ShortText | None = None
    finding_ids: list[FindingId] = Field(default_factory=list)
    assessment_id: AssessmentId | None = None
    warnings: list[ShortText] = Field(default_factory=list)
    usage: TaskUsage | None = None
    model_id: str | None = Field(default=None, max_length=200)
    prompt_version: str | None = Field(default=None, max_length=200)
    completed_at: UtcDatetime | None = None
    error: TaskError | None = None

    @model_validator(mode="after")
    def _status_rules(self) -> TaskResult:
        s = self.status
        if s is AgentTaskStatus.FAILED and self.error is None:
            raise ValueError("FAILED requires error {category, code}")
        if self.error is not None and s not in {AgentTaskStatus.FAILED, AgentTaskStatus.REJECTED}:
            raise ValueError(f"error is only allowed on FAILED/REJECTED, not {s}")
        if s is not AgentTaskStatus.COMPLETED and (self.finding_ids or self.assessment_id):
            raise ValueError("finding_ids/assessment_id are only reported on COMPLETED")
        if len(set(self.finding_ids)) != len(self.finding_ids):
            raise ValueError("finding_ids must not contain duplicates")
        if self.completed_at is not None and not s.is_terminal:
            raise ValueError("completed_at is only set on a terminal status")
        return self


class AgentCard(ValueModel):
    """Minimal agent card at ``/.well-known/agent-card.json`` (§12.2, ADR-004 C3; ``llm_mode``
    per ADR-006 E3). Not the A2A card shape (ADR-004 C4 parked)."""

    id: AgentId
    role: ShortText
    task_types: list[TaskType] = Field(min_length=1)
    version: str = Field(min_length=1, max_length=64)
    clearance: Classification
    llm_mode: LlmMode


__all__ = [
    "AgentCard",
    "AgentTaskStatus",
    "TaskError",
    "TaskRequest",
    "TaskResult",
    "TaskSubmitResponse",
    "TaskType",
    "TaskUsage",
    "TraceContext",
]
