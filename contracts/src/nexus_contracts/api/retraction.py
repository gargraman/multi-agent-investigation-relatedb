"""Evidence retraction, I2 (spec §9.4, §10 I2, §12.1, §12.3; ADR-005 D4).

``POST /v1/investigations/{id}/evidence/{evidence_id}/retract`` (proxied by the orchestrator at
``/api/v1/...``). The ids come from the path; the operator identity travels in the
``X-Nexus-Actor`` header, not the body. The spec defines no request body or impact-set shape;
these are the smallest shapes that carry what §10 I2 says the response returns.
"""

from __future__ import annotations

from pydantic import Field

from nexus_contracts.common import AssessmentId, FindingId, ManifestId, ValueModel


class RetractEvidenceRequest(ValueModel):
    reason: str | None = Field(default=None, min_length=1, max_length=500)


class RetractionImpact(ValueModel):
    """Impact set: everything that depends on the retracted evidence, at any depth (§10 I2).
    Findings and assessments are now ``NEEDS_REVIEW``; assertions drop out of I4 with their
    findings (ADR-007 G11)."""

    evidence_id: ManifestId
    decision_id: str = Field(min_length=1, max_length=128)
    finding_ids: list[FindingId] = Field(default_factory=list)
    assertion_ids: list[str] = Field(default_factory=list)
    assessment_ids: list[AssessmentId] = Field(default_factory=list)
