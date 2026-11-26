"""scripts/verify_openai.py post-parse check path, on canned outputs (no network, no key)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from nexus_sigint.schemas import SigintLlmOutput

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    script = REPO_ROOT / "scripts" / "verify_openai.py"
    spec = importlib.util.spec_from_file_location("verify_openai", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["verify_openai"] = mod
    spec.loader.exec_module(mod)
    return mod


vo = _load()


def _output(statement: str, reasoning: str = "Three records on distinct days.") -> SigintLlmOutput:
    item: dict[str, Any] = {
        "candidate_id": "C1",
        "pattern_label": "RECURRING_CONTACT",
        "confidence": 0.8,
        "statement": statement,
        "reasoning_summary": reasoning,
    }
    return SigintLlmOutput.model_validate({"classifications": [item]})


def test_probe_output_citing_its_own_handles_and_evidence_passes() -> None:
    out = _output("HDL-ALPHA-1 and HDL-CHARLIE-1 in repeated contact.", "Per ev-probe-1.")
    assert vo.probe_violations(out) == []


def test_probe_output_citing_a_foreign_handle_fails() -> None:
    assert vo.probe_violations(_output("HDL-ALPHA-1 linked to HDL-BRAVO-1.")) == [
        "C1.statement references an id outside its candidate"
    ]
