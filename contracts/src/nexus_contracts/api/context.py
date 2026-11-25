"""Context Service investigation lifecycle (spec §12.3; ADR-002 A1/A2; ADR-007 G5).

``POST /v1/investigations`` (orchestrator only) creates the ``Investigation`` node and registers
the scenario's canonical entities, ``USED_BY`` relationships and evidence references, all in one
Relate transaction. Entities and evidence are stored per investigation with
``uid = {investigation_id}:{manifest_id}`` (ADR-007 G5).

Idempotent on ``investigation_id``: the Context Service stores ``content_hash()`` on the node.
Same id + same hash → ``200`` with ``created=false``; same id + different hash →
``409 content_conflict`` (ProblemDetails). The orchestrator builds this body from the manifest
with :meth:`ContextInvestigationCreate.from_manifest`, so both sides hash the same thing.

``POST /v1/investigations/{id}/close`` fences further writes (``409 investigation_cancelled``).
Idempotent: closing a closed investigation returns ``200`` with the original ``closed_at``.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import Field

from nexus_contracts.api.states import InvestigationStatus
from nexus_contracts.common import (
    AgentId,
    Classification,
    EntityType,
    InvestigationId,
    ManifestId,
    ShortText,
    UtcDatetime,
    ValueModel,
)
from nexus_contracts.scenario.manifest import ScenarioManifest


class CartesianPoint(ValueModel):
    """Cell centre on the fictional grid (cartesian, not WGS-84; spec §9.3)."""

    x: float
    y: float


class EntityRegistration(ValueModel):
    manifest_id: ManifestId
    type: EntityType
    display_name: ShortText
    aliases: list[str] = Field(default_factory=list)
    position: CartesianPoint | None = None  # LOCATION only


class RelationshipRegistration(ValueModel):
    """Manifest-sourced ``RELATED_TO`` edge; P0 has only ``USED_BY`` (HANDLE → PERSON)."""

    type: str = Field(pattern=r"^USED_BY$")
    source: ManifestId
    target: ManifestId


class EvidenceRegistration(ValueModel):
    manifest_id: ManifestId
    owner_node: AgentId
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification: Classification


class ContextInvestigationCreate(ValueModel):
    investigation_id: InvestigationId
    scenario_id: ManifestId
    scenario_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    objective: ShortText
    grid_pitch: float = Field(gt=0, description="Needed by I4: co-location radius = 1.5 × pitch.")
    entities: list[EntityRegistration] = Field(min_length=1)
    relationships: list[RelationshipRegistration] = Field(default_factory=list)
    evidence: list[EvidenceRegistration] = Field(min_length=1)

    @classmethod
    def from_manifest(
        cls, investigation_id: str, manifest: ScenarioManifest, objective: str | None = None
    ) -> ContextInvestigationCreate:
        return cls(
            investigation_id=investigation_id,
            scenario_id=manifest.scenario_id,
            scenario_version=manifest.version,
            objective=objective or manifest.objective,
            grid_pitch=manifest.grid.pitch,
            entities=[
                EntityRegistration(
                    manifest_id=e.id,
                    type=e.type,
                    display_name=e.display_name,
                    aliases=list(e.aliases),
                    position=CartesianPoint(x=e.position.x, y=e.position.y) if e.position else None,
                )
                for e in manifest.entities
            ],
            relationships=[
                RelationshipRegistration(type="USED_BY", source=e.id, target=e.used_by)
                for e in manifest.entities
                if e.used_by
            ],
            evidence=[
                EvidenceRegistration(
                    manifest_id=ev.id,
                    owner_node=ev.owner_node,
                    sha256=ev.sha256,
                    classification=ev.classification,
                )
                for ev in manifest.evidence
            ],
        )

    def content_hash(self) -> str:
        """sha256 of canonical JSON (sorted keys, no whitespace; lists sorted canonically)."""
        body = self.model_dump(mode="json")
        for key in ("entities", "relationships", "evidence"):
            body[key] = sorted(body[key], key=lambda x: json.dumps(x, sort_keys=True))
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ContextInvestigationCreated(ValueModel):
    investigation_id: InvestigationId
    created: bool = Field(description="False when an identical create was replayed.")
    entity_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)


class ContextInvestigationClose(ValueModel):
    status: InvestigationStatus = Field(description="Terminal status being recorded.")

    def model_post_init(self, _: object) -> None:
        if not self.status.is_terminal:
            raise ValueError("close requires a terminal investigation status")


class ContextInvestigationClosed(ValueModel):
    investigation_id: InvestigationId
    status: InvestigationStatus
    closed_at: UtcDatetime
