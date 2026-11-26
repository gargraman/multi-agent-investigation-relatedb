from __future__ import annotations

from pathlib import Path

from nexus_contracts.scenario.fixtures import SightingLog
from nexus_contracts.scenario.loader import load_manifest
from nexus_contracts.scenario.manifest import ScenarioManifest

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = ROOT / "data/scenarios"
FIXTURES = ROOT / "data/fixtures"


def manifest_for(scenario: str) -> ScenarioManifest:
    return load_manifest(SCENARIOS / scenario)


def load_logs(scenario: str) -> dict[str, SightingLog]:
    """Every GEOINT sighting log of a scenario, keyed by evidence id."""
    logs = {}
    for path in sorted((FIXTURES / "geoint" / scenario).glob("ev-geo-*.json")):
        log = SightingLog.model_validate_json(path.read_text())
        logs[log.evidence_id] = log
    return logs
