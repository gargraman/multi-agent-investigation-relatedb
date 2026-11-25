"""scripts/gen_scenario.py: deterministic, drift-checked, never overwrites hand-authored signal."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data"
SCRIPT = REPO_ROOT / "scripts" / "gen_scenario.py"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def data_copy(tmp_path: Path) -> Path:
    shutil.copytree(DATA, tmp_path / "data")
    return tmp_path / "data"


def all_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def noise_files(data: Path, scenario: str) -> list[Path]:
    cfg = yaml.safe_load((data / "scenarios" / scenario / "generator.yaml").read_text())
    return [
        data / "fixtures" / node / scenario / f"{eid}.json"
        for node in ("sigint", "geoint")
        for eid in cfg["noise"][node]["ids"]
    ]


def test_committed_pack_matches_fresh_regeneration() -> None:
    result = run("--check")
    assert result.returncode == 0, result.stderr


def test_regeneration_is_byte_identical(data_copy: Path) -> None:
    before = all_bytes(data_copy)
    for f in noise_files(data_copy, "baseline-corroboration"):
        f.unlink()  # regenerate from scratch, not just "no-op on unchanged files"
    result = run("--data-root", str(data_copy))
    assert result.returncode == 0, result.stderr
    assert all_bytes(data_copy) == before


def test_check_fails_on_modified_noise_fixture(data_copy: Path) -> None:
    target = noise_files(data_copy, "contradiction")[0]
    target.write_text(target.read_text().replace('"synthetic": true', '"synthetic": true '))
    result = run("--check", "--data-root", str(data_copy))
    assert result.returncode == 1
    assert target.name in result.stderr


def test_check_fails_on_signal_edit_without_sha_refresh(data_copy: Path) -> None:
    signal = data_copy / "fixtures/sigint/baseline-corroboration/ev-sig-004.json"
    signal.write_text(signal.read_text().replace("07:15", "07:16"))
    result = run("--check", "--data-root", str(data_copy))
    assert result.returncode == 1
    assert "manifest.yaml" in result.stderr


def test_write_restores_noise_and_never_touches_signal(data_copy: Path) -> None:
    signal = data_copy / "fixtures/geoint/baseline-corroboration/ev-geo-002.json"
    edited = signal.read_text().replace("238.4", "239.4")
    signal.write_text(edited)
    noise = noise_files(data_copy, "baseline-corroboration")[0]
    original_noise = noise.read_bytes()
    noise.write_text("{}")

    assert run("--data-root", str(data_copy)).returncode == 0
    assert signal.read_text() == edited  # hand-authored signal preserved
    assert noise.read_bytes() == original_noise  # noise regenerated
    assert run("--check", "--data-root", str(data_copy)).returncode == 0  # sha refreshed
