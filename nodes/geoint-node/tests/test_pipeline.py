from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.findings import PlacedAtAssertion
from nexus_contracts.common import AgentId, Classification, ConfidenceBand
from nexus_contracts.scenario.fixtures import SightingLog
from nexus_contracts.scenario.oracle_check import check_agent_subset, find_forbidden
from nexus_geoint import pipeline
from nexus_geoint.pipeline import run, stored_summary
from nexus_geoint.schemas import (
    REASONING_MAX,
    SCHEMA_VERSION,
    STATEMENT_MAX,
    PatternLabel,
)
from nexus_shared.agent.llm_steps import FENCE_CLOSE, FENCE_OPEN
from nexus_shared.llm import TokenBudget

from .conftest import (
    FIXTURES,
    ScenarioCopy,
    ScriptedProvider,
    load_logs,
    make_request,
    manifest_for,
    valid_output_for,
)

BASELINE = "baseline-corroboration"
PI = "prompt-injection"


async def _run(provider, request=None, manifest=None, fixtures=FIXTURES, budget=15000):
    request = request or make_request()
    return await run(
        request,
        provider=provider,
        manifest=manifest or manifest_for(request.scenario_id),
        fixtures_root=fixtures,
        budget=TokenBudget(budget),
    )


def test_schema_constants() -> None:
    assert SCHEMA_VERSION == "geoint-llm-output@1.0.0"
    assert STATEMENT_MAX == 300 and REASONING_MAX == 500
    assert {p.value for p in PatternLabel} == {
        "CO_LOCATION",
        "DWELL_OVERLAP",
        "CONFLICTING_PLACEMENT",
        "INCONCLUSIVE",
    }
    assert pipeline.AGENT_ID is AgentId.GEOINT
    assert pipeline.TASK_TYPES == ("analyze_synthetic_geospatial",)
    assert pipeline.LLM_TASK == "geoint-classify"


@pytest.mark.parametrize("scenario", [BASELINE, "contradiction", PI])
async def test_pipeline_matches_oracle_subset_with_scripted_llm(scenario: str) -> None:
    outcome = await _run(ScriptedProvider(valid_output_for), request=make_request(scenario))
    assert outcome.status is AgentTaskStatus.COMPLETED, outcome.error
    assert outcome.batch is not None
    oracle = manifest_for(scenario).oracle
    assert check_agent_subset(oracle, AgentId.GEOINT, outcome.batch.findings) == []


async def test_findings_are_detector_facts() -> None:
    provider = ScriptedProvider(lambda d: valid_output_for(d, confidence=0.81234567))
    out = await _run(provider)
    assert out.status is AgentTaskStatus.COMPLETED and out.error is None
    assert out.prompt_version == "geoint-classify@1.0.0" and out.model_id == "fake-model"
    assert out.usage.llm_calls == 1 and out.candidate_count == 3
    f1 = out.batch.findings[0]
    assert f1.about == ["ENT-DELTA", "ENT-ECHO", "LOC-GRID-04"]
    assert f1.kind.value == "OBSERVATION"
    assert f1.confidence == 0.8123 and f1.confidence_band is ConfidenceBand.HIGH
    assert f1.classification is Classification.DEMO_LOW
    assert f1.schema_version == SCHEMA_VERSION
    assert [e.evidence_id for e in f1.evidence] == ["ev-geo-011"]
    assert f1.evidence[0].owner_node_id == "geoint-agent"
    assert all(isinstance(a, PlacedAtAssertion) for a in f1.assertions)
    assert {a.location for a in f1.assertions} == {"LOC-GRID-04"}
    assert {a.subject for a in f1.assertions} == {"ENT-DELTA", "ENT-ECHO"}
    assert f1.valid_from == min(a.valid_from for a in f1.assertions)
    assert f1.valid_until == max(a.valid_until for a in f1.assertions)
    # Deterministic summary: counts, labels and dropped points; no LLM text.
    assert "3 placement candidate(s)" in out.summary and "3 CO_LOCATION" in out.summary
    assert "unresolved" in out.summary and "off-grid" in out.summary
    assert "observed together" not in out.summary


async def test_llm_input_carries_candidates_and_fenced_summaries() -> None:
    provider = ScriptedProvider(valid_output_for)
    await _run(provider, request=make_request("contradiction"))
    data = provider.calls[0]["input_data"]
    c1, c2 = data["candidates"]
    assert [c1["candidate_id"], c2["candidate_id"]] == ["C1", "C2"]
    assert c1["pattern"] == ["CONFLICT"] and c1["location"] == "LOC-GRID-02"
    assert c1["subjects"] == ["ENT-ALPHA"]
    assert isinstance(c1["valid_from"], str) and isinstance(c1["sightings"][0]["start"], str)
    summary = c1["evidence"][0]["summary"]
    assert summary.startswith(FENCE_OPEN) and summary.endswith(FENCE_CLOSE)
    text = json.dumps(data)
    assert "tsk_" not in text and "inv_" not in text and "sensor" not in text


async def test_injection_cannot_move_placements_or_reach_stored_summaries() -> None:
    provider = ScriptedProvider(valid_output_for)
    out = await _run(provider, request=make_request(PI))
    assert out.status is AgentTaskStatus.COMPLETED
    locations = {a.location for f in out.batch.findings for a in f.assertions}
    assert locations == {"LOC-GRID-09"}
    assert all("LOC-GRID-00" not in f.about for f in out.batch.findings)
    planted = load_logs(PI)["ev-geo-005"].summary
    stored = [e.summary for f in out.batch.findings for e in f.evidence]
    # No 20-character run of the fixture text survives into a stored summary. (Both texts open
    # with the generic 18 characters "Synthetic sighting", the mandated stored-summary prefix.)
    windows = {planted[i : i + 20] for i in range(len(planted) - 19)}
    assert not any(w in s for w in windows for s in stored)
    assert all("TRIPWIRE" not in s and "LOC-GRID-00" not in s for s in stored)
    oracle = manifest_for(PI).oracle
    assert find_forbidden(oracle, [("stored", s) for s in stored]) == []
    # The planted text reached the model only inside exactly one fence.
    fenced = provider.calls[0]["input_data"]["candidates"][0]["evidence"][0]["summary"]
    assert fenced.count(FENCE_OPEN) == 1 and fenced.count(FENCE_CLOSE) == 1
    assert "NEXUS-TRIPWIRE-G2L" in fenced


async def test_obeying_model_cannot_change_graph_facts() -> None:
    clean = await _run(ScriptedProvider(valid_output_for), request=make_request(PI))
    obeying = ScriptedProvider(
        lambda d: valid_output_for(d, confidence=1.0, statement="Relocated to LOC-GRID-00.")
    )
    out = await _run(obeying, request=make_request(PI))
    for a, b in zip(clean.batch.findings, out.batch.findings, strict=True):
        assert (a.about, a.assertions, a.evidence) == (b.about, b.assertions, b.evidence)
        assert (a.valid_from, a.valid_until) == (b.valid_from, b.valid_until)


def test_stored_summary_is_derived() -> None:
    log = load_logs(PI)["ev-geo-005"]
    assert stored_summary("ev-geo-005", log) == (
        "Synthetic sighting log ev-geo-005: 2 observations, 2026-10-26 to 2026-10-26."
    )
    one = SightingLog.model_validate(
        log.model_dump(mode="json", by_alias=True)
        | {"summary": "IGNORE THIS"}
        | {"observations": [log.observations[0].model_dump(mode="json")]}
    )
    assert stored_summary("ev-geo-005", one) == (
        "Synthetic sighting log ev-geo-005: 1 observation, 2026-10-26 to 2026-10-26."
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"input_refs": ["ev-sig-001"]},  # foreign (SIGINT-owned) evidence
        {"input_refs": ["ev-nope"]},
        {"task_type": "analyze_synthetic_communications"},
        {"contract_version": "2.0"},
    ],
)
async def test_rejects_foreign_evidence_and_wrong_task_type(kw: dict) -> None:
    provider = ScriptedProvider(valid_output_for)
    out = await _run(provider, request=make_request(**kw))
    assert out.status is AgentTaskStatus.REJECTED
    assert out.error.code is ErrorCode.TASK_REJECTED and provider.calls == []


async def test_integrity_error_on_sha_mismatch(tmp_path: Path) -> None:
    copy = ScenarioCopy(tmp_path, BASELINE)
    copy.edit_fixture("ev-geo-011", lambda d: d.update(sensor="tampered"))
    provider = ScriptedProvider(valid_output_for)
    out = await _run(provider, manifest=copy.manifest, fixtures=copy.fixtures)
    assert out.status is AgentTaskStatus.FAILED
    assert out.error.code is ErrorCode.EVIDENCE_INTEGRITY_ERROR and provider.calls == []


async def test_zero_candidates_completes_without_batch() -> None:
    provider = ScriptedProvider(valid_output_for)
    out = await _run(provider, request=make_request(input_refs=["ev-geo-001"]))
    assert out.status is AgentTaskStatus.COMPLETED and out.batch is None
    assert provider.calls == [] and out.usage.llm_calls == 0
    assert "No placement candidates" in out.summary


async def test_ceiling_drops_candidates_with_warning(tmp_path: Path) -> None:
    copy = ScenarioCopy(tmp_path, BASELINE)
    copy.edit_fixture("ev-geo-011", lambda d: d.update(classification="DEMO-MID"), rehash=True)
    for ev in copy.raw["evidence"]:
        if ev["id"] == "ev-geo-011":
            ev["classification"] = "DEMO-MID"
    copy.save()
    request = make_request(classification_ceiling="DEMO-LOW")
    provider = ScriptedProvider(valid_output_for)
    out = await _run(provider, request=request, manifest=copy.manifest, fixtures=copy.fixtures)
    assert out.status is AgentTaskStatus.COMPLETED
    assert [f.about[-1] for f in out.batch.findings] == ["LOC-GRID-14", "LOC-GRID-15"]
    assert out.warnings and "ceiling" in out.warnings[0]
    sent = json.dumps(provider.calls[0]["input_data"])
    assert "LOC-GRID-04" not in sent and "ev-geo-011" not in sent


async def test_inconclusive_label_still_emits_finding() -> None:
    provider = ScriptedProvider(lambda d: valid_output_for(d, pattern_label="INCONCLUSIVE"))
    out = await _run(provider)
    assert out.status is AgentTaskStatus.COMPLETED and len(out.batch.findings) == 3
    assert "3 INCONCLUSIVE" in out.summary


async def test_repair_on_bad_output_then_schema_invalid() -> None:
    bad = {"classifications": []}
    out = await _run(ScriptedProvider(bad, valid_output_for))
    assert out.status is AgentTaskStatus.COMPLETED and out.usage.llm_calls == 2
    out = await _run(ScriptedProvider(bad, bad))
    assert out.error.code is ErrorCode.SCHEMA_INVALID and out.batch is None
