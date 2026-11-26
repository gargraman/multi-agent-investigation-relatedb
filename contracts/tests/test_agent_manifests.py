"""``data/agent-manifests/<id>.json``: the oracle-free projection the agents mount (S3 spec §10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from nexus_contracts.scenario import AgentManifest, ScenarioManifest, load_agent_manifest

DATA = Path(__file__).resolve().parents[2] / "data"
SCENARIO_IDS = sorted(p.name for p in (DATA / "scenarios").iterdir() if p.is_dir())
AGENT_FILES = sorted((DATA / "agent-manifests").glob("*.json"))


def test_one_agent_manifest_per_scenario() -> None:
    assert SCENARIO_IDS
    assert [p.stem for p in AGENT_FILES] == SCENARIO_IDS


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.stem)
def test_agent_manifest_has_no_oracle(path: Path) -> None:
    assert "oracle" not in path.read_text().lower()
    assert "oracle" not in json.loads(path.read_text())


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.stem)
def test_agent_manifest_equals_manifest_minus_oracle(path: Path) -> None:
    full = yaml.safe_load((DATA / "scenarios" / path.stem / "manifest.yaml").read_text())
    expected = {k: v for k, v in full.items() if k != "oracle"}
    assert "oracle" in full  # the source still carries it
    assert json.loads(path.read_text()) == expected


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.stem)
def test_agent_manifest_loads_and_matches_the_full_manifest(path: Path) -> None:
    agent = load_agent_manifest(path)
    full = ScenarioManifest.model_validate(
        yaml.safe_load((DATA / "scenarios" / path.stem / "manifest.yaml").read_text())
    )
    assert agent.model_dump() == full.model_dump(exclude={"oracle"})
    assert "oracle" not in AgentManifest.model_fields


def test_agent_manifest_model_rejects_an_oracle_key() -> None:
    raw = yaml.safe_load((DATA / "scenarios" / SCENARIO_IDS[0] / "manifest.yaml").read_text())
    with pytest.raises(ValueError, match="oracle"):
        AgentManifest.model_validate(raw)
