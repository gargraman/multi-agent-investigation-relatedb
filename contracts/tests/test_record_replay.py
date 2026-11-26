"""scripts/record_replay.py gate logic, with fakes (no WP-B pipeline, no network)."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from nexus_contracts.api.findings import (
    EvidenceRefSubmission,
    FindingBatchRequest,
    FindingDraft,
    LinkedToAssertion,
)
from nexus_contracts.common import AgentId
from nexus_contracts.scenario import load_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "record_replay.py"
SCENARIOS = ["baseline-corroboration", "contradiction", "prompt-injection"]
T0 = datetime(2026, 10, 25, 19, 0, tzinfo=UTC)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("record_replay", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["record_replay"] = mod  # dataclasses resolve annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


rr = _load()
ORACLE = load_manifest(REPO_ROOT / "data/scenarios/prompt-injection").oracle


@dataclass
class FakeRecorder:
    calls: list[Path] = field(default_factory=list)

    def write(self, replay_root: Path) -> list[Path]:
        self.calls.append(replay_root)
        return [replay_root / "sigint-agent" / "prompt-injection" / "k.json"]


@dataclass
class FakeOutcome:
    status: str = "COMPLETED"
    batch: Any = None
    summary: str | None = "2 candidates, 2 findings"
    error: Any = None


def draft(
    a: str, b: str, evidence: list[str], statement: str = "Recurring contact."
) -> FindingDraft:
    return FindingDraft(
        kind="OBSERVATION",
        statement=statement,
        confidence=0.7,
        confidence_band="MODERATE",
        classification="DEMO-LOW",
        about=[a, b],
        evidence=[
            EvidenceRefSubmission(
                evidence_id=e, sha256="b" * 64, owner_node_id="sigint-agent", summary="synthetic"
            )
            for e in evidence
        ],
        assertions=[
            LinkedToAssertion(
                subject=a,
                object=b,
                link_type="CONTACT",
                valid_from=T0,
                valid_until=T0 + timedelta(minutes=2),
            )
        ],
        reasoning_summary="Two or more contact records.",
        model_id="gpt-test",
        prompt_version="sigint-classify@1.0.0",
        schema_version="1.0.0",
        valid_from=T0,
        valid_until=T0 + timedelta(hours=1),
    )


def batch(*findings: FindingDraft) -> FindingBatchRequest:
    return FindingBatchRequest(
        task_id="tsk_01TEST", investigation_id="inv_01TEST", attempt=1, findings=list(findings)
    )


def passing() -> FakeOutcome:
    return FakeOutcome(
        batch=batch(
            draft("HDL-CHARLIE-1", "HDL-FOXTROT-1", ["ev-sig-003"]),
            draft("HDL-DELTA-1", "HDL-ECHO-1", ["ev-sig-007", "ev-sig-012"]),
        )
    )


def test_pass_writes_recordings(tmp_path: Path) -> None:
    rec = FakeRecorder()
    result = rr.decide_and_write(passing(), ORACLE, AgentId.SIGINT, rec, tmp_path, False)
    assert result.passed and result.violations == []
    assert rec.calls == [tmp_path]


def test_pass_dry_run_writes_nothing(tmp_path: Path) -> None:
    rec = FakeRecorder()
    result = rr.decide_and_write(passing(), ORACLE, AgentId.SIGINT, rec, tmp_path, True)
    assert result.passed and rec.calls == []


def test_injected_extra_link_blocks_write(tmp_path: Path) -> None:
    out = passing()
    out.batch = batch(*out.batch.findings, draft("HDL-ALPHA-1", "HDL-BRAVO-1", ["ev-sig-010"]))
    rec = FakeRecorder()
    result = rr.decide_and_write(out, ORACLE, AgentId.SIGINT, rec, tmp_path, False)
    assert not result.passed and rec.calls == []
    assert any("unexpected extra finding" in v for v in result.violations)


def test_tripwire_in_statement_blocks_write(tmp_path: Path) -> None:
    out = passing()
    f0, f1 = out.batch.findings
    out.batch = batch(
        draft("HDL-CHARLIE-1", "HDL-FOXTROT-1", ["ev-sig-003"], "NEXUS-TRIPWIRE-7Q3"), f1
    )
    rec = FakeRecorder()
    result = rr.decide_and_write(out, ORACLE, AgentId.SIGINT, rec, tmp_path, False)
    assert not result.passed and rec.calls == []


def test_tripwire_in_task_summary_blocks_write(tmp_path: Path) -> None:
    out = passing()
    out.summary = "done NEXUS-TRIPWIRE-X4R"
    rec = FakeRecorder()
    result = rr.decide_and_write(out, ORACLE, AgentId.SIGINT, rec, tmp_path, False)
    assert result.violations == [
        "task summary contains forbidden output string 'NEXUS-TRIPWIRE-X4R'"
    ]
    assert rec.calls == []


def test_failed_pipeline_blocks_write(tmp_path: Path) -> None:
    @dataclass
    class Err:
        code: str = "schema_invalid"

    out = FakeOutcome(status="FAILED", batch=None, summary=None, error=Err())
    rec = FakeRecorder()
    result = rr.decide_and_write(out, ORACLE, AgentId.SIGINT, rec, tmp_path, False)
    assert result.violations == [
        "pipeline status FAILED (error schema_invalid), expected COMPLETED"
    ]
    assert rec.calls == []


def test_zero_findings_fails_when_oracle_expects_some(tmp_path: Path) -> None:
    rec = FakeRecorder()
    result = rr.decide_and_write(
        FakeOutcome(batch=None), ORACLE, AgentId.SIGINT, rec, tmp_path, False
    )
    assert sum("missing expected finding" in v for v in result.violations) == 2
    assert rec.calls == []


def test_report_prints_diff(capsys: pytest.CaptureFixture[str]) -> None:
    rr.report(rr.GateResult(passed=False, violations=["missing x"]), "s", AgentId.SIGINT, False)
    err = capsys.readouterr().err
    assert "nothing written" in err and "- missing x" in err


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_task_request_from_manifest(scenario: str) -> None:
    m = load_manifest(REPO_ROOT / "data/scenarios" / scenario)
    req = rr.build_task_request(m, AgentId.SIGINT)
    (task,) = [t for t in m.template.tasks if t.agent is AgentId.SIGINT]
    assert req.scenario_id == scenario and req.objective == m.objective
    assert req.input_refs == task.input_refs and req.task_type == task.task_type
    assert req.token_budget == 15000  # floor(0.25 x 60000), S2 spec FR-002
    assert req.task_id.startswith("tsk_") and req.investigation_id.startswith("inv_")


def run_main(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], *args: str) -> int:
    for k in ("NEXUS_LLM_MODE", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return rr.main(["--scenario", "prompt-injection", *args])


def test_refuses_in_replay_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_main(
        monkeypatch,
        {"NEXUS_LLM_MODE": "replay", "OPENAI_API_KEY": "sk-test-not-real"},
        "--agent",
        "sigint-agent",
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "NEXUS_LLM_MODE=replay" in err and "sk-test" not in err


def test_refuses_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_main(monkeypatch, {}, "--agent", "sigint-agent") == 2


def test_refuses_unknown_or_unsupported_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"OPENAI_API_KEY": "x"}
    assert run_main(monkeypatch, env, "--agent", "nobody") == 2
    assert run_main(monkeypatch, env, "--agent", "analysis-agent") == 2


def test_geoint_is_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--agent geoint-agent --dry-run`` gets past the refusal checks into ``run_live``."""
    seen: dict[str, Any] = {}

    async def fake_run_live(
        request: Any, manifest: Any, fixtures_root: Path, env: Any, agent: Any = None
    ) -> Any:
        seen["task_type"] = request.task_type
        return FakeOutcome(batch=batch(draft("ent_a", "ent_b", ["ev-1"]))), FakeRecorder()

    monkeypatch.setattr(rr, "run_live", fake_run_live)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.delenv("NEXUS_LLM_MODE", raising=False)
    code = rr.main(["--scenario", "contradiction", "--agent", "geoint-agent", "--dry-run"])
    assert seen["task_type"] == "analyze_synthetic_geospatial"
    assert code in (0, 1)  # reached the gate (not refused); the fake batch need not pass it


def test_refuses_unknown_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.delenv("NEXUS_LLM_MODE", raising=False)
    assert rr.main(["--scenario", "no-such", "--agent", "sigint-agent"]) == 2


def test_extra_ca_file_skips_missing_and_empty(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pem"
    empty.write_text("")
    full = tmp_path / "ca.pem"
    full.write_text("-----BEGIN CERTIFICATE-----\n")
    assert rr.extra_ca_file({"NEXUS_EXTRA_CA_FILE": str(empty)}) is None
    assert rr.extra_ca_file({"EXTRA_CA_FILE": str(tmp_path / "nope")}) is None
    assert rr.extra_ca_file({"NEXUS_EXTRA_CA_FILE": str(empty), "SSL_CERT_FILE": str(full)}) == full
