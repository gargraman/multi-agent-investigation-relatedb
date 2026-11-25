"""Evidence integrity: every fixture exists, matches its manifest sha256 and fixture shape."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_contracts.scenario import (
    CommsLog,
    CommsRecord,
    EvidenceIntegrityError,
    Observation,
    load_evidence,
    load_manifest,
    load_scenario,
    sha256_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data"
SCENARIOS = ["baseline-corroboration", "contradiction", "prompt-injection"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_all_evidence_verifies(scenario: str) -> None:
    loaded = load_scenario(DATA / "scenarios" / scenario, DATA / "fixtures")
    assert set(loaded.fixtures) == {e.id for e in loaded.manifest.evidence}
    for ref in loaded.manifest.evidence:
        assert sha256_file(DATA / "fixtures" / ref.file) == ref.sha256


@pytest.fixture
def data_copy(tmp_path: Path) -> Path:
    shutil.copytree(DATA, tmp_path / "data")
    return tmp_path / "data"


def test_tampered_file_fails_integrity(data_copy: Path) -> None:
    m = load_manifest(data_copy / "scenarios" / "baseline-corroboration")
    ref = m.evidence_ref("ev-sig-004")
    path = data_copy / "fixtures" / ref.file
    path.write_text(path.read_text().replace("07:15", "07:16"))
    with pytest.raises(EvidenceIntegrityError, match="sha256 mismatch"):
        load_evidence(ref, data_copy / "fixtures")


def test_missing_file_fails_integrity(data_copy: Path) -> None:
    m = load_manifest(data_copy / "scenarios" / "contradiction")
    ref = m.evidence_ref("ev-geo-003")
    (data_copy / "fixtures" / ref.file).unlink()
    with pytest.raises(EvidenceIntegrityError, match="not found"):
        load_evidence(ref, data_copy / "fixtures")
    assert EvidenceIntegrityError.code == "evidence_integrity_error"


def test_fixture_rejects_extra_fields_and_non_utc() -> None:
    doc = json.loads((DATA / "fixtures/sigint/baseline-corroboration/ev-sig-004.json").read_text())
    CommsLog.model_validate(doc)
    with pytest.raises(ValidationError):
        CommsLog.model_validate({**doc, "raw_audio": "nope"})
    rec = dict(doc["records"][0], start="2026-10-12T09:15:00+02:00")
    with pytest.raises(ValidationError, match="UTC"):
        CommsRecord.model_validate(rec)
    with pytest.raises(ValidationError):
        CommsRecord.model_validate(
            dict(doc["records"][0], to_handle=doc["records"][0]["from_handle"])
        )


def test_observation_window_is_half_open_and_ordered() -> None:
    base = {
        "observation_id": "o1",
        "subject": "subject-alpha",
        "x": 1.0,
        "y": 1.0,
        "observed_from": "2026-10-14T09:00:00Z",
        "observed_until": "2026-10-14T09:00:00Z",
    }
    with pytest.raises(ValidationError):
        Observation.model_validate(base)
