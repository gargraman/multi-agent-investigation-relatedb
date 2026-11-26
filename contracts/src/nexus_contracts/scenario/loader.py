"""Load a scenario manifest and verify its evidence fixtures (spec §11.3 steps 1 and 3).

``load_manifest`` parses and validates metadata only (what the orchestrator needs).
``load_evidence`` is the node-side check: file exists, sha256 matches the manifest, the JSON
parses as the owner's fixture shape, and its header agrees with the manifest entry. A mismatch
raises ``EvidenceIntegrityError`` (task error code ``evidence_integrity_error``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from nexus_contracts.common import AgentId
from nexus_contracts.scenario.fixtures import CommsLog, Fixture, SightingLog
from nexus_contracts.scenario.manifest import AgentManifest, EvidenceRef, ScenarioManifest

MANIFEST_FILENAME = "manifest.yaml"


class EvidenceIntegrityError(Exception):
    """An evidence file is missing, altered, or disagrees with its manifest entry."""

    code = "evidence_integrity_error"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path) -> ScenarioManifest:
    """Parse ``manifest.yaml`` (or a scenario directory containing one) and validate it."""
    if path.is_dir():
        path = path / MANIFEST_FILENAME
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return ScenarioManifest.model_validate(raw)


def load_agent_manifest(path: Path) -> AgentManifest:
    """Parse and validate one oracle-free ``data/agent-manifests/<id>.json`` (agent runtime)."""
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return AgentManifest.model_validate(raw)


def load_evidence(ref: EvidenceRef, fixtures_root: Path) -> Fixture:
    """Load one evidence fixture, verifying existence, sha256 and header consistency."""
    path = fixtures_root / ref.file
    if not path.is_file():
        raise EvidenceIntegrityError(f"{ref.id}: file not found: {ref.file}")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != ref.sha256:
        raise EvidenceIntegrityError(f"{ref.id}: sha256 mismatch for {ref.file}")
    model = CommsLog if ref.owner_node is AgentId.SIGINT else SightingLog
    fixture = model.model_validate(json.loads(data))
    if fixture.evidence_id != ref.id:
        raise EvidenceIntegrityError(
            f"{ref.id}: fixture declares evidence_id {fixture.evidence_id}"
        )
    if fixture.classification != ref.classification:
        raise EvidenceIntegrityError(f"{ref.id}: classification differs from the manifest")
    return fixture


@dataclass(frozen=True)
class LoadedScenario:
    manifest: ScenarioManifest
    fixtures: dict[str, Fixture]


def load_scenario(scenario_dir: Path, fixtures_root: Path) -> LoadedScenario:
    """Load the manifest and every evidence fixture; also check that every GEOINT point snaps to
    a declared LOCATION cell, so the detector can always resolve it."""
    manifest = load_manifest(scenario_dir)
    fixtures = {ref.id: load_evidence(ref, fixtures_root) for ref in manifest.evidence}
    for evidence_id, fixture in fixtures.items():
        if isinstance(fixture, SightingLog):
            for obs in fixture.observations:
                cell = manifest.grid.cell_of(obs.x, obs.y)
                if cell is None or manifest.location_at_cell(*cell) is None:
                    raise EvidenceIntegrityError(
                        f"{evidence_id}/{obs.observation_id}: point is not in a declared cell"
                    )
    return LoadedScenario(manifest=manifest, fixtures=fixtures)
