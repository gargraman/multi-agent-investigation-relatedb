"""Pure helpers for ``POST /v1/assessments`` (S4a FR-209..212). No I/O."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from nexus_contracts.api.assessments import AssessmentSubmission, EdgeType, ProposedEdge
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.common import FindingKind, FindingStatus


class CitationRejected(Exception):
    """A used finding id is not in the task's receipt or not visible (FR-211). Carries nothing:
    the response must not say which id missed, or why (§9.5)."""


def assessment_key(task_id: str) -> str:
    """Idempotency key of an assessment submission (FR-210, §8.5)."""
    return f"assessment:{task_id}"


def used_finding_ids(body: AssessmentSubmission) -> list[str]:
    """The ``USES_FINDING`` set (FR-209): every conclusion's ``supporting_finding_ids`` and every
    ``contradictions[].finding_ids``, de-duplicated and sorted."""
    ids = {fid for c in body.conclusions for fid in c.supporting_finding_ids}
    ids.update(fid for c in body.contradictions for fid in c.finding_ids)
    return sorted(ids)


def assessment_hash(body: AssessmentSubmission) -> str:
    """sha256 of the canonical JSON of the body minus ``attempt`` (FR-210): sorted keys, no
    whitespace, UTF-8. Attempts of one task with identical content compare equal."""
    canon = json.dumps(
        body.model_dump(mode="json", exclude={"attempt"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FindingFacts:
    """What edge validation needs to know about a finding the caller may see."""

    kind: FindingKind
    status: FindingStatus


def validate_edge(
    edge: ProposedEdge, receipt: set[str], visible: dict[str, FindingFacts]
) -> ErrorCode | None:
    """The pure part of FR-213: ``None`` if the edge may be written, else the rejection code.

    Both endpoints must be in the task's ``receipt`` and in ``visible`` (the receipt rows within
    the caller's clearance), else ``unknown_finding_reference``: the same test, and the same
    single code, as the citation check, so existence is never leaked (§9.5). ``DERIVED_FROM``
    additionally needs an ``INFERENCE`` source (``schema_invalid``). The cycle check needs the
    graph and lives in the repository transaction."""
    for fid in (edge.source_finding_id, edge.target_finding_id):
        if fid not in receipt or fid not in visible:
            return ErrorCode.UNKNOWN_FINDING_REFERENCE
    if (
        edge.type is EdgeType.DERIVED_FROM
        and visible[edge.source_finding_id].kind is not FindingKind.INFERENCE
    ):
        return ErrorCode.SCHEMA_INVALID
    return None
