"""Every committed replay recording passes the S3b output guard (statement/reasoning text).

Candidates are rebuilt by running the agent's own pipeline in replay mode on a request from
``scripts/record_replay.py``'s ``build_task_request``, so aliases and allowed refs come from the
real detector path. The pipeline's ``check_output`` is wrapped only to capture those arguments;
the recording's ``response`` is then parsed and checked directly.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_contracts.common import AgentId
from nexus_contracts.scenario import load_manifest
from nexus_shared.llm import ReplayLLMProvider, TokenBudget

REPO_ROOT = Path(__file__).resolve().parents[2]
REPLAY_ROOT = REPO_ROOT / "data" / "replay"
RECORDINGS = sorted(REPLAY_ROOT.glob("*/*/*.json"))


def _load_record_replay() -> ModuleType:
    script = REPO_ROOT / "scripts" / "record_replay.py"
    spec = importlib.util.spec_from_file_location("record_replay", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["record_replay"] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


rr = _load_record_replay()


def _agent_modules(agent: AgentId) -> tuple[ModuleType, ModuleType]:
    if agent is AgentId.GEOINT:
        from nexus_geoint import pipeline, schemas
    else:
        from nexus_sigint import pipeline, schemas
    return pipeline, schemas


def test_recordings_exist() -> None:
    assert RECORDINGS, "no replay recordings found"


@pytest.mark.parametrize("path", RECORDINGS, ids=lambda p: "/".join(p.parts[-3:-1]))
async def test_recording_passes_output_guard(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = json.loads(path.read_text(encoding="utf-8"))
    material = record["key_material"]
    if material["call_ordinal"] != 0:
        pytest.skip("only primary (call_ordinal 0) recordings are gated")
    agent = AgentId(material["agent_id"])
    pipeline, schemas = _agent_modules(agent)

    captured: list[tuple[list[str], dict[str, frozenset[str]]]] = []
    real_check = pipeline.check_output

    def spy(output: Any, candidate_ids: list[str], allowed_refs: Any) -> list[str]:
        captured.append((list(candidate_ids), dict(allowed_refs)))
        return real_check(output, candidate_ids, allowed_refs)

    monkeypatch.setattr(pipeline, "check_output", spy)
    manifest = load_manifest(REPO_ROOT / "data" / "scenarios" / material["scenario_id"])
    request = rr.build_task_request(manifest, agent)
    outcome = await pipeline.run(
        request,
        provider=ReplayLLMProvider(REPLAY_ROOT),
        manifest=manifest,
        fixtures_root=REPO_ROOT / "data" / "fixtures",
        budget=TokenBudget(request.token_budget),
    )
    assert captured, f"{path.name}: pipeline never reached check_output ({outcome.error})"
    candidate_ids, allowed_refs = captured[0]
    assert candidate_ids == [c["candidate_id"] for c in material["normalized_input"]["candidates"]]

    output_schema = schemas.GeointLlmOutput if agent is AgentId.GEOINT else schemas.SigintLlmOutput
    response = output_schema.model_validate(record["response"])
    assert schemas.check_output(response, candidate_ids, allowed_refs) == []
    # Accepted end to end on the primary call alone.
    assert outcome.status is AgentTaskStatus.COMPLETED, outcome.error
    assert outcome.usage.llm_calls == 1
