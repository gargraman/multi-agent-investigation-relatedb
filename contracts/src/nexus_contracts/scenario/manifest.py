"""Scenario manifest model: ``data/scenarios/<id>/manifest.yaml`` (spec §11.3).

The manifest is metadata only: the fictional grid, the canonical entities, references (path +
sha256) to evidence fixtures and the oracle. Raw evidence lives in ``data/fixtures/<node>/<id>/``
and is read only by its owning node. This module validates everything that can be checked
without touching the filesystem; ``loader.py`` adds the file-existence and sha256 checks.
"""

from __future__ import annotations

import math
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_contracts.common import AgentId, Classification, EntityType
from nexus_contracts.scenario.oracle import EvidencePair, Oracle

MANIFEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

# Fixture directory per evidence owner (spec §11.3: data/fixtures/<node>/<id>/).
NODE_FIXTURE_DIR: dict[AgentId, str] = {AgentId.SIGINT: "sigint", AgentId.GEOINT: "geoint"}

# Co-location radius in pitches (ADR-007 G6): same cell or one of the 8 neighbours.
NEIGHBOUR_RADIUS_PITCHES = 1.5


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Point(_Model):
    """A cartesian point in abstract grid units (fictional, not WGS-84)."""

    x: float
    y: float


class Grid(_Model):
    """Fictional cartesian grid (ADR-007 G6). Cell (col, row) spans
    ``[origin + col*pitch, origin + (col+1)*pitch)`` on x, likewise on y."""

    origin: Point
    pitch: float = Field(gt=0)
    cols: int = Field(gt=0)
    rows: int = Field(gt=0)

    @property
    def neighbour_radius(self) -> float:
        return NEIGHBOUR_RADIUS_PITCHES * self.pitch

    def cell_of(self, x: float, y: float) -> tuple[int, int] | None:
        """Snap a point to its (col, row), or ``None`` if it lies outside the grid."""
        col = math.floor((x - self.origin.x) / self.pitch)
        row = math.floor((y - self.origin.y) / self.pitch)
        if 0 <= col < self.cols and 0 <= row < self.rows:
            return col, row
        return None

    def cell_centre(self, col: int, row: int) -> Point:
        return Point(
            x=self.origin.x + (col + 0.5) * self.pitch,
            y=self.origin.y + (row + 0.5) * self.pitch,
        )

    def centre_cell(self, p: Point) -> tuple[int, int] | None:
        """(col, row) if ``p`` is exactly a cell centre inside the grid, else ``None``."""
        cell = self.cell_of(p.x, p.y)
        if cell is None:
            return None
        c = self.cell_centre(*cell)
        if math.isclose(c.x, p.x, abs_tol=1e-9) and math.isclose(c.y, p.y, abs_tol=1e-9):
            return cell
        return None


class ManifestEntity(_Model):
    """A canonical fictional entity (spec §9.1). IDs are stable and never aliased (ADR-006 E1)."""

    id: str = Field(pattern=MANIFEST_ID_PATTERN)
    type: EntityType
    display_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    position: Point | None = Field(default=None, description="LOCATION only: the cell centre.")
    used_by: str | None = Field(default=None, description="HANDLE only: the PERSON using it.")

    @model_validator(mode="after")
    def _type_specific_fields(self) -> ManifestEntity:
        if (self.type is EntityType.LOCATION) != (self.position is not None):
            raise ValueError(
                f"{self.id}: position is required for LOCATION and forbidden otherwise"
            )
        if self.used_by is not None and self.type is not EntityType.HANDLE:
            raise ValueError(f"{self.id}: used_by is only allowed on HANDLE entities")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError(f"{self.id}: duplicate alias")
        return self


class EvidenceRef(_Model):
    """Reference to one evidence fixture. ``file`` is relative to ``data/fixtures/``."""

    id: str = Field(pattern=MANIFEST_ID_PATTERN)
    owner_node: AgentId
    file: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    classification: Classification

    @model_validator(mode="after")
    def _file_under_owner(self) -> EvidenceRef:
        path = PurePosixPath(self.file)
        if path.is_absolute() or ".." in path.parts or "\\" in self.file:
            raise ValueError(f"{self.id}: file must be a relative POSIX path without '..'")
        node_dir = NODE_FIXTURE_DIR.get(self.owner_node)
        if node_dir is None:
            raise ValueError(f"{self.id}: {self.owner_node} does not own evidence")
        if path.parts[0] != node_dir:
            raise ValueError(
                f"{self.id}: file must live under {node_dir}/ (owner {self.owner_node})"
            )
        if path.suffix != ".json":
            raise ValueError(f"{self.id}: fixtures are JSON files")
        return self


class TemplateTask(_Model):
    """One task the orchestrator creates for an investigation (spec §6.2, §8.5)."""

    agent: AgentId
    task_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    token_share: float = Field(gt=0, le=1, description="Reservation of the token cap (§8.5).")
    input_refs: list[str] = Field(default_factory=list, description="Evidence IDs.")


class Template(_Model):
    tasks: list[TemplateTask] = Field(min_length=1)


class AgentManifest(_Model):
    """The oracle-free manifest projection that specialist agents mount (S3 spec §10).

    Everything an agent needs (grid, entities, evidence references, template) and nothing it must
    never see: the oracle. ``data/agent-manifests/<id>.json`` is generated from the full manifest
    by ``scripts/gen_scenario.py``. ``extra="forbid"`` means an ``oracle`` key is rejected here.
    """

    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    synthetic: Literal[True]
    objective: str = Field(min_length=1)
    template: Template
    grid: Grid
    entities: list[ManifestEntity] = Field(min_length=1)
    evidence: list[EvidenceRef] = Field(min_length=1)

    # -- lookups -----------------------------------------------------------------------------

    def entity(self, entity_id: str) -> ManifestEntity:
        return self._entities()[entity_id]

    def _entities(self) -> dict[str, ManifestEntity]:
        return {e.id: e for e in self.entities}

    def evidence_ref(self, evidence_id: str) -> EvidenceRef:
        return {e.id: e for e in self.evidence}[evidence_id]

    def alias_index(self) -> dict[str, str]:
        """Exact-match resolution table (spec §11.4 step 1): id or alias -> entity id."""
        index: dict[str, str] = {}
        for e in self.entities:
            for name in (e.id, *e.aliases):
                index[name] = e.id
        return index

    def handles_of(self, person_id: str) -> list[str]:
        return [e.id for e in self.entities if e.used_by == person_id]

    def location_at_cell(self, col: int, row: int) -> str | None:
        for e in self.entities:
            if e.position is not None and self.grid.centre_cell(e.position) == (col, row):
                return e.id
        return None

    # -- cross-field validation --------------------------------------------------------------

    @model_validator(mode="after")
    def _validate_references(self) -> AgentManifest:
        self._check_entities()
        self._check_evidence_and_template()
        return self

    def _check_entities(self) -> None:
        ids = [e.id for e in self.entities]
        _require_unique(ids, "entity id")
        entities = self._entities()

        names: dict[str, str] = {}
        for e in self.entities:
            for name in (e.id, *e.aliases):
                key = name.casefold()
                if key in names and names[key] != e.id:
                    raise ValueError(f"name {name!r} is ambiguous: {names[key]} and {e.id}")
                names[key] = e.id

        cells: dict[tuple[int, int], str] = {}
        for e in self.entities:
            if e.used_by is not None:
                target = entities.get(e.used_by)
                if target is None:
                    raise ValueError(f"{e.id}: used_by {e.used_by!r} is not a manifest entity")
                if target.type is not EntityType.PERSON:
                    raise ValueError(f"{e.id}: used_by must target a PERSON, got {target.type}")
            if e.position is not None:
                cell = self.grid.centre_cell(e.position)
                if cell is None:
                    raise ValueError(f"{e.id}: position is not a cell centre inside the grid")
                if cell in cells:
                    raise ValueError(f"{e.id}: cell {cell} already used by {cells[cell]}")
                cells[cell] = e.id

    def _check_evidence_and_template(self) -> None:
        _require_unique([e.id for e in self.evidence], "evidence id")
        _require_unique([e.file for e in self.evidence], "evidence file")
        _require_unique([e.id for e in self.entities] + [e.id for e in self.evidence], "id")

        agents = [t.agent for t in self.template.tasks]
        _require_unique(agents, "template agent")
        total_share = sum(t.token_share for t in self.template.tasks)
        if total_share > 1.0 + 1e-9:
            raise ValueError(f"template token_share sums to {total_share:.3f} > 1.0")

        owners = {e.id: e.owner_node for e in self.evidence}
        referenced: list[str] = []
        for task in self.template.tasks:
            for ref in task.input_refs:
                if ref not in owners:
                    raise ValueError(f"template {task.agent}: unknown evidence {ref!r}")
                if owners[ref] != task.agent:
                    raise ValueError(f"template {task.agent}: {ref} is owned by {owners[ref]}")
                referenced.append(ref)
        _require_unique(referenced, "template input_ref")
        unused = set(owners) - set(referenced)
        if unused:
            raise ValueError(f"evidence not referenced by any template task: {sorted(unused)}")


class ScenarioManifest(AgentManifest):
    """``data/scenarios/<id>/manifest.yaml`` (spec §11.3)."""

    # The full manifest = the agent projection (AgentManifest) plus the oracle.
    oracle: Oracle

    @model_validator(mode="after")
    def _validate_oracle(self) -> ScenarioManifest:
        self._check_oracle()
        return self

    def _check_oracle(self) -> None:
        entities = self._entities()
        owners = {e.id: e.owner_node for e in self.evidence}

        def need_entity(ref: str, where: str) -> ManifestEntity:
            if ref not in entities:
                raise ValueError(f"oracle {where}: unknown entity {ref!r}")
            return entities[ref]

        def need_evidence(ref: str, where: str) -> AgentId:
            if ref not in owners:
                raise ValueError(f"oracle {where}: unknown evidence {ref!r}")
            return owners[ref]

        for f in self.oracle.expected_findings:
            types = [need_entity(r, "expected_findings").type for r in f.about]
            for ref in f.evidence:
                if need_evidence(ref, "expected_findings") != f.agent:
                    raise ValueError(f"oracle expected_findings: {ref} is not owned by {f.agent}")
            if f.assertion == "LINKED_TO" and (
                f.agent is not AgentId.SIGINT
                or len(types) != 2
                or any(t is not EntityType.HANDLE for t in types)
            ):
                raise ValueError("oracle: LINKED_TO is asserted by SIGINT between two HANDLEs")
            if f.assertion == "PLACED_AT" and (
                f.agent is not AgentId.GEOINT
                or not any(t in (EntityType.PERSON, EntityType.ASSET) for t in types)
            ):
                raise ValueError("oracle: PLACED_AT is asserted by GEOINT about a PERSON or ASSET")

        def check_pairs(pairs: list[EvidencePair], where: str, cross_agent: bool) -> None:
            for p in pairs:
                for ref in p.about:
                    need_entity(ref, where)
                pair_owners = {need_evidence(ref, where) for ref in p.evidence}
                if cross_agent and len(pair_owners) != 2:
                    raise ValueError(f"oracle {where}: pair {p.evidence} must span two agents")

        check_pairs(self.oracle.corroborations, "corroborations", cross_agent=True)
        check_pairs(self.oracle.contradictions, "contradictions", cross_agent=False)

        for hp in self.oracle.hidden_paths:
            for role in ("a", "via"):
                ent = need_entity(getattr(hp, role), "hidden_paths")
                if ent.type is not EntityType.PERSON or not self.handles_of(ent.id):
                    raise ValueError(
                        f"oracle hidden_paths: {role}={ent.id} must be a PERSON with a handle"
                    )
            b = need_entity(hp.b, "hidden_paths")
            if b.type not in (EntityType.PERSON, EntityType.ASSET):
                raise ValueError(f"oracle hidden_paths: b={b.id} must be a PERSON or ASSET")

        for d in self.oracle.expected_denials:
            for ref in d.evidence:
                need_evidence(ref, "expected_denials")


def _require_unique(values: list[str], what: str) -> None:
    seen: set[str] = set()
    dupes = sorted({v for v in values if v in seen or seen.add(v)})  # type: ignore[func-returns-value]
    if dupes:
        raise ValueError(f"duplicate {what}: {dupes}")
