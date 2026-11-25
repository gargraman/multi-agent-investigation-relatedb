"""Assessments (spec §6.4, §8.5 "Assessment idempotency", §9.4, §12.3, §12.4; ADR-003 B6).

- ``AssessmentSubmission``: the single idempotent ``POST /v1/assessments`` body, keyed on
  ``task_id``. It carries the assessment, its ``USES_FINDING`` set (implied by the conclusions'
  ``supporting_finding_ids``) and the proposed ``CORROBORATES``/``CONTRADICTS``/``DERIVED_FROM``
  edges. Contradiction *status changes* are not a separate field: per §9.4 both findings move to
  ``DISPUTED`` deterministically when the Context Service validates a ``CONTRADICTS`` edge.
- ``AssessmentSubmissionResponse``: the ``assessment_id`` plus accepted and individually
  rejected edges (§8.5: invalid edges don't fail the whole assessment).
- ``Assessment``: the read model; the §12.4 example round-trips through it.

Local validation: confidence in [0, 1]; ids unique within a list; an edge never points at its
own source; a contradiction names two distinct findings. Whether a cited finding exists, belongs
to this investigation, is visible to the caller and was in this run's retrieval (the ID
validator, §6.4) is checked by the agent and re-checked by the Context Service.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.common import (
    AssessmentId,
    Attempt,
    Confidence,
    ConfidenceBand,
    FindingId,
    FindingKind,
    InvestigationId,
    ShortText,
    TaskId,
    ValueModel,
)


class AssessmentStatus(StrEnum):
    """Same lifecycle as Finding (§9.2). An assessment is created ``ACTIVE``; there is no
    ``DRAFT`` state (spec v1.3 corrected the §12.4 example)."""

    ACTIVE = "ACTIVE"
    DISPUTED = "DISPUTED"
    SUPERSEDED = "SUPERSEDED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class EdgeType(StrEnum):
    """Finding→finding edges Analysis may propose (§9.3, §12.3)."""

    CORROBORATES = "CORROBORATES"
    CONTRADICTS = "CONTRADICTS"
    DERIVED_FROM = "DERIVED_FROM"


def _unique(values: list[str], name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{name} must not contain duplicates")


class Conclusion(ValueModel):
    statement: ShortText
    kind: FindingKind
    confidence: Confidence
    confidence_band: ConfidenceBand
    supporting_finding_ids: list[FindingId] = Field(min_length=1)
    limitations: list[ShortText] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids(self) -> Conclusion:
        _unique(self.supporting_finding_ids, "supporting_finding_ids")
        return self


class Contradiction(ValueModel):
    finding_ids: list[FindingId] = Field(min_length=2, max_length=2)
    description: ShortText
    effect_on_confidence: ShortText

    @model_validator(mode="after")
    def _pair(self) -> Contradiction:
        _unique(self.finding_ids, "finding_ids")
        return self


class ProposedEdge(ValueModel):
    """``(source)-[:type]->(target)``. Shape is not in the spec (only the edge types are)."""

    type: EdgeType
    source_finding_id: FindingId
    target_finding_id: FindingId

    @model_validator(mode="after")
    def _no_self_loop(self) -> ProposedEdge:
        if self.source_finding_id == self.target_finding_id:
            raise ValueError("an edge must connect two different findings")
        return self


class _AssessmentBody(ValueModel):
    summary: ShortText
    conclusions: list[Conclusion] = Field(min_length=1)
    contradictions: list[Contradiction] = Field(default_factory=list)
    information_gaps: list[ShortText] = Field(default_factory=list)
    follow_up_questions: list[ShortText] = Field(
        default_factory=list, description="§6.4; absent from the §12.4 example."
    )
    model_id: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=200)


class AssessmentSubmission(_AssessmentBody):
    """``POST /v1/assessments`` body; idempotency key = ``task_id`` (§8.5, ADR-003 B6)."""

    task_id: TaskId
    investigation_id: InvestigationId
    attempt: Attempt
    schema_version: str = Field(min_length=1, max_length=64)
    proposed_edges: list[ProposedEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _distinct_edges(self) -> AssessmentSubmission:
        keys = [(e.type, e.source_finding_id, e.target_finding_id) for e in self.proposed_edges]
        if len(set(keys)) != len(keys):
            raise ValueError("proposed_edges must not repeat an edge")
        return self


class RejectedEdge(ValueModel):
    """An edge the Context Service refused. A missing, foreign or above-clearance finding all
    report ``unknown_finding_reference`` so existence is not leaked (§9.5 404-not-403 rule)."""

    edge: ProposedEdge
    code: ErrorCode


class AssessmentSubmissionResponse(ValueModel):
    """Same key + same hash → original ``assessment_id``; different hash → ``409
    already_committed`` (ProblemDetails with ``assessment_id``) (§8.5)."""

    assessment_id: AssessmentId
    accepted_edges: list[ProposedEdge] = Field(default_factory=list)
    rejected_edges: list[RejectedEdge] = Field(default_factory=list)


class Assessment(_AssessmentBody):
    """Read model (``GET /api/v1/investigations/{id}/assessment``); matches §12.4."""

    assessment_id: AssessmentId
    investigation_id: InvestigationId
    status: AssessmentStatus
