"""Importing a node's ``main`` module needs no NEXUS_* / service env vars: ``app`` is a lazy module
attribute (PEP 562). Checked in a clean subprocess, because in-process the environment and modules
are already warm and a regression would pass vacuously."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["nexus_geoint.main", "nexus_sigint.main"])
def test_main_imports_without_env(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module} as m; assert 'app' not in vars(m)"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
