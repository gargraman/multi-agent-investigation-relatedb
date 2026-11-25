"""Scenario catalog: ``<scenarios_dir>/<id>/manifest.yaml`` (spec §11.3).

The orchestrator reads manifests only (ids and metadata), never evidence fixtures. File I/O runs
in a worker thread (ADR-005 D2). A manifest that fails validation is skipped with a warning, so
one broken pack cannot take the API down.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pydantic import BaseModel, ValidationError

from nexus_contracts.scenario.loader import MANIFEST_FILENAME, load_manifest
from nexus_contracts.scenario.manifest import ScenarioManifest

log = logging.getLogger(__name__)


class ScenarioSummary(BaseModel):
    """``GET /api/v1/scenarios`` item. Metadata only: no entities, evidence or oracle."""

    id: str
    version: str
    objective: str
    task_count: int


class ScenarioList(BaseModel):
    items: list[ScenarioSummary]


def _scan(root: Path) -> dict[str, ScenarioManifest]:
    found: dict[str, ScenarioManifest] = {}
    if not root.is_dir():
        log.warning("scenarios_dir does not exist", extra={"path": str(root)})
        return found
    for path in sorted(root.glob(f"*/{MANIFEST_FILENAME}")):
        try:
            manifest = load_manifest(path)
        except (OSError, ValueError, ValidationError) as exc:
            log.warning(
                "skipping invalid scenario manifest",
                extra={"path": str(path), "error": type(exc).__name__},
            )
            continue
        found[manifest.scenario_id] = manifest
    return found


class ScenarioCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def all(self) -> dict[str, ScenarioManifest]:
        return await asyncio.to_thread(_scan, self.root)

    async def get(self, scenario_id: str) -> ScenarioManifest | None:
        return (await self.all()).get(scenario_id)

    async def summaries(self) -> ScenarioList:
        return ScenarioList(
            items=[
                ScenarioSummary(
                    id=m.scenario_id,
                    version=m.version,
                    objective=m.objective,
                    task_count=len(m.template.tasks),
                )
                for m in (await self.all()).values()
            ]
        )
