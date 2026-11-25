"""Scenario manifest model: both committed manifests load; validators reject bad manifests."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from nexus_contracts.common import AgentId, EntityType
from nexus_contracts.scenario import Grid, Point, ScenarioManifest, load_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = REPO_ROOT / "data" / "scenarios"


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    return yaml.safe_load((SCENARIOS / "baseline-corroboration" / "manifest.yaml").read_text())


def mutated(raw: dict[str, Any], fn) -> dict[str, Any]:  # noqa: ANN001
    doc = copy.deepcopy(raw)
    fn(doc)
    return doc


def entity(doc: dict[str, Any], entity_id: str) -> dict[str, Any]:
    return next(e for e in doc["entities"] if e["id"] == entity_id)


@pytest.mark.parametrize(
    "scenario", ["baseline-corroboration", "contradiction", "prompt-injection"]
)
def test_committed_manifest_loads(scenario: str) -> None:
    m = load_manifest(SCENARIOS / scenario)
    assert m.scenario_id == scenario
    assert m.synthetic is True
    assert 20 <= len(m.evidence) <= 40
    assert {t.agent for t in m.template.tasks} == set(AgentId)
    assert m.entity("HDL-ALPHA-1").used_by == "ENT-ALPHA"
    assert m.alias_index()["kestrel-7@relay.example.invalid"] == "HDL-ALPHA-1"
    assert m.location_at_cell(2, 2) == "LOC-GRID-14"


def test_everything_is_fictional_and_reserved() -> None:
    for scenario in ("baseline-corroboration", "contradiction", "prompt-injection"):
        m = load_manifest(SCENARIOS / scenario)
        for e in m.entities:
            if e.type is EntityType.HANDLE:
                assert all(a.endswith(".example.invalid") for a in e.aliases), e.id


def test_grid_snapping_and_neighbour_radius() -> None:
    g = Grid(origin=Point(x=0, y=0), pitch=100, cols=6, rows=4)
    assert g.cell_of(238.4, 261.0) == (2, 2)
    assert g.cell_of(600.0, 10.0) is None
    assert g.centre_cell(Point(x=250, y=250)) == (2, 2)
    assert g.centre_cell(Point(x=251, y=250)) is None
    assert g.neighbour_radius == 150


CASES = {
    "synthetic must be true": lambda d: d.update(synthetic=False),
    "unknown top-level field": lambda d: d.update(region="x"),
    "duplicate entity id": lambda d: d["entities"].append(copy.deepcopy(d["entities"][0])),
    "used_by targets non-person": lambda d: entity(d, "HDL-ALPHA-1").update(used_by="AST-VAN-1"),
    "used_by unknown": lambda d: entity(d, "HDL-ALPHA-1").update(used_by="ENT-NOBODY"),
    "used_by on non-handle": lambda d: entity(d, "AST-VAN-1").update(used_by="ENT-ALPHA"),
    "position not a centre": lambda d: entity(d, "LOC-GRID-14").update(
        position={"x": 251, "y": 250}
    ),
    "position outside grid": lambda d: entity(d, "LOC-GRID-14").update(
        position={"x": 650, "y": 50}
    ),
    "position on non-location": lambda d: entity(d, "ENT-ALPHA").update(
        position={"x": 50, "y": 50}
    ),
    "location without position": lambda d: entity(d, "LOC-GRID-14").pop("position"),
    "two locations one cell": lambda d: entity(d, "LOC-GRID-15").update(
        position={"x": 250, "y": 250}
    ),
    "ambiguous alias": lambda d: entity(d, "ENT-BRAVO").update(aliases=["SUBJECT-ALPHA"]),
    "duplicate evidence id": lambda d: d["evidence"].append(copy.deepcopy(d["evidence"][0])),
    "bad sha256": lambda d: d["evidence"][0].update(sha256="xyz"),
    "file outside owner dir": lambda d: d["evidence"][0].update(file="geoint/x/ev.json"),
    "path traversal": lambda d: d["evidence"][0].update(file="sigint/../../etc/x.json"),
    "token shares exceed 1": lambda d: d["template"]["tasks"][0].update(token_share=0.9),
    "input_ref of other owner": lambda d: d["template"]["tasks"][0]["input_refs"].append(
        "ev-geo-001"
    ),
    "unreferenced evidence": lambda d: d["template"]["tasks"][0]["input_refs"].pop(),
    "oracle unknown entity": lambda d: d["oracle"]["hidden_paths"][0].update(b="ENT-NOBODY"),
    "oracle path via asset": lambda d: d["oracle"]["hidden_paths"][0].update(via="AST-VAN-1"),
    "oracle unknown evidence": lambda d: d["oracle"]["corroborations"][0].update(
        evidence=["ev-sig-009", "ev-nope"]
    ),
    "corroboration same agent": lambda d: d["oracle"]["corroborations"][0].update(
        evidence=["ev-sig-009", "ev-sig-004"]
    ),
    "LINKED_TO about persons": lambda d: d["oracle"]["expected_findings"][0].update(
        about=["ENT-ALPHA", "ENT-CHARLIE"]
    ),
    "short canary": lambda d: d["oracle"].update(canary_strings=["abc"]),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_validator_rejects(raw: dict[str, Any], case: str) -> None:
    with pytest.raises(ValidationError):
        ScenarioManifest.model_validate(mutated(raw, CASES[case]))


def test_baseline_raw_is_valid(raw: dict[str, Any]) -> None:
    ScenarioManifest.model_validate(raw)
