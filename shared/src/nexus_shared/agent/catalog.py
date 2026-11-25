"""Scenario manifest catalog shared by specialist agents."""

from __future__ import annotations

import logging
from pathlib import Path

from nexus_contracts.scenario.loader import load_manifest
from nexus_contracts.scenario.manifest import ScenarioManifest

log = logging.getLogger(__name__)


class ManifestCatalog:
    """Loads ``SCENARIOS_DIR/<scenario_id>/manifest.yaml`` once per scenario (metadata only)."""

    def __init__(self, scenarios_dir: Path) -> None:
        self.scenarios_dir = scenarios_dir
        self._cache: dict[str, ScenarioManifest] = {}

    def get(self, scenario_id: str) -> ScenarioManifest | None:
        if scenario_id in self._cache:
            return self._cache[scenario_id]
        path = self.scenarios_dir / scenario_id / "manifest.yaml"
        if not path.is_file():  # scenario_id is a ManifestId (no '/' or '..')
            return None
        try:
            manifest = load_manifest(path)
        except Exception:
            log.exception("manifest failed to load", extra={"scenario_id": scenario_id})
            return None
        self._cache[scenario_id] = manifest
        return manifest

    def is_ready(self) -> bool:
        return self.scenarios_dir.is_dir() and any(self.scenarios_dir.glob("*/manifest.yaml"))
