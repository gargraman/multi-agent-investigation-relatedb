"""Fails the suite if committed JSON Schemas drift from the Pydantic contracts."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_committed_schemas_match_models() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_schemas.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"schema drift (run `make schemas`):\n{result.stderr}"
