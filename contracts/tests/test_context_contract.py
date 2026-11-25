from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_contracts.api.context import ContextInvestigationClose, ContextInvestigationCreate
from nexus_contracts.scenario.loader import load_manifest

ROOT = Path(__file__).resolve().parents[2]


def _manifest():
    return load_manifest(ROOT / "data/scenarios/baseline-corroboration/manifest.yaml")


def test_from_manifest_carries_entities_used_by_and_evidence() -> None:
    m = _manifest()
    body = ContextInvestigationCreate.from_manifest("inv_test1", m)
    assert len(body.entities) == len(m.entities)
    assert len(body.evidence) == len(m.evidence)
    assert {(r.source, r.target) for r in body.relationships} >= {
        ("HDL-ALPHA-1", "ENT-ALPHA"),
        ("HDL-CHARLIE-1", "ENT-CHARLIE"),
    }
    assert body.grid_pitch == m.grid.pitch


def test_content_hash_stable_and_sensitive() -> None:
    m = _manifest()
    a = ContextInvestigationCreate.from_manifest("inv_test1", m)
    b = ContextInvestigationCreate.model_validate(
        {
            **a.model_dump(mode="json"),
            "entities": list(reversed(a.model_dump(mode="json")["entities"])),
        }
    )
    assert a.content_hash() == b.content_hash()
    c = ContextInvestigationCreate.from_manifest("inv_test1", m, objective="Something else")
    assert c.content_hash() != a.content_hash()


def test_close_requires_terminal_status() -> None:
    assert ContextInvestigationClose(status="CANCELLED").status == "CANCELLED"
    with pytest.raises(ValidationError):
        ContextInvestigationClose(status="RUNNING")
