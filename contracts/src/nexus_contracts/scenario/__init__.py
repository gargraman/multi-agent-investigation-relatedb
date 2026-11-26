"""Scenario pack contracts: manifest, oracle, evidence fixtures and loader (spec §11)."""

from nexus_contracts.scenario.fixtures import (
    GEOINT_SCHEMA,
    SIGINT_SCHEMA,
    CommsLog,
    CommsRecord,
    Fixture,
    Observation,
    SightingLog,
)
from nexus_contracts.scenario.loader import (
    EvidenceIntegrityError,
    LoadedScenario,
    load_agent_manifest,
    load_evidence,
    load_manifest,
    load_scenario,
    sha256_file,
)
from nexus_contracts.scenario.manifest import (
    AgentManifest,
    EvidenceRef,
    Grid,
    ManifestEntity,
    Point,
    ScenarioManifest,
    Template,
    TemplateTask,
)
from nexus_contracts.scenario.oracle import (
    EvidencePair,
    ExpectedDenial,
    ExpectedFinding,
    HiddenPath,
    Oracle,
)

__all__ = [
    "GEOINT_SCHEMA",
    "SIGINT_SCHEMA",
    "AgentManifest",
    "CommsLog",
    "CommsRecord",
    "EvidenceIntegrityError",
    "EvidencePair",
    "EvidenceRef",
    "ExpectedDenial",
    "ExpectedFinding",
    "Fixture",
    "Grid",
    "HiddenPath",
    "LoadedScenario",
    "ManifestEntity",
    "Observation",
    "Oracle",
    "Point",
    "ScenarioManifest",
    "SightingLog",
    "Template",
    "TemplateTask",
    "load_agent_manifest",
    "load_evidence",
    "load_manifest",
    "load_scenario",
    "sha256_file",
]
