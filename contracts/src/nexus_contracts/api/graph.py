"""Graph-value reads: provenance I1, contradictions I3, hidden links I4, graph export I6
(S4a spec §8; ADR-007 G11).

All are read models served by the Context Service and proxied by the orchestrator for the
operator. Every record is filtered to the caller's clearance. The operator views (I1 provenance,
I3 contradictions, I6 graph) show ``RETRACTED`` / ``NEEDS_REVIEW`` records with their status;
trusted-only filtering (``ACTIVE`` / ``DISPUTED``) applies to analysis reads (``/context``,
``/search``) and to the I4 hidden-link computation, whose paths rest on trusted findings only.
The spec fixes only the model names; the field sets are the smallest that carry what each query
returns.

- ``Provenance`` (I1): assessment → findings → evidence, with each evidence item's hash, owner
  and ``ACTIVE``/``RETRACTED`` status.
- ``ContradictionPair`` (I3): two findings joined by an accepted ``CONTRADICTS`` edge. Ordered
  (``finding_a < finding_b``) so a pair has one canonical form and never repeats.
- ``HiddenLink`` (I4): ``entity_a`` and ``entity_b`` have no direct link but both co-locate with
  ``via`` in time windows. ``HiddenLinkWindow`` serialises ``from_`` as ``"from"``.
- ``Graph`` (I6): nodes and edges for the operator's graph view; ``props`` are scalars only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from nexus_contracts.common import (
    AssessmentId,
    Classification,
    FindingId,
    FindingStatus,
    ManifestId,
    UtcDatetime,
    ValueModel,
)

# --- I1 provenance ---


class ProvenanceEvidence(ValueModel):
    evidence_id: ManifestId
    sha256: str
    owner_node_id: str
    status: Literal["ACTIVE", "RETRACTED"]


class ProvenanceFinding(ValueModel):
    finding_id: FindingId
    source_agent_id: str
    status: FindingStatus
    classification: Classification
    evidence: list[ProvenanceEvidence]


class Provenance(ValueModel):
    assessment_id: AssessmentId
    findings: list[ProvenanceFinding]


# --- I3 contradictions ---


class ContradictionPair(ValueModel):
    finding_a: FindingId
    finding_b: FindingId

    @model_validator(mode="after")
    def _ordered(self) -> ContradictionPair:
        if not self.finding_a < self.finding_b:
            raise ValueError("finding_a must sort before finding_b")
        return self


# --- I4 hidden links ---


class HiddenLinkWindow(ValueModel):
    """Time window in which the ``via`` entity was co-located with the two cells ``cell_c`` and
    ``cell_b`` (I4 corroboration; ``from``/``until`` are UTC)."""

    cell_c: ManifestId
    cell_b: ManifestId
    from_: UtcDatetime = Field(alias="from")
    until: UtcDatetime


class HiddenLink(ValueModel):
    entity_a: ManifestId
    via: ManifestId
    entity_b: ManifestId
    sigint_findings: list[FindingId]
    geoint_findings: list[FindingId]
    windows: list[HiddenLinkWindow]
    disputed: bool = Field(description="True when any supporting finding is ``DISPUTED``.")


# --- I6 graph export ---


class GraphNodeLabel(StrEnum):
    INVESTIGATION = "Investigation"
    ENTITY = "Entity"
    EVIDENCE = "Evidence"
    FINDING = "Finding"
    ASSERTION = "Assertion"
    ASSESSMENT = "Assessment"
    DECISION = "Decision"


class GraphNode(ValueModel):
    id: str
    label: GraphNodeLabel
    props: dict[str, str | float | int | bool | None]


class GraphEdge(ValueModel):
    source: str
    target: str
    type: str


class Graph(ValueModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
