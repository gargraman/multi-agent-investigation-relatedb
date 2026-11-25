from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from nexus_contracts.a2a.task import TaskRequest, TaskUsage
from nexus_contracts.scenario.loader import load_manifest
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_shared.llm import (
    LLMOutputInvalidError,
    LLMResult,
    ReplayKeyContext,
    TokenBudget,
)
from nexus_shared.llm.schema import OutputParseError, parse_output

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = ROOT / "data/scenarios"
FIXTURES = ROOT / "data/fixtures"


def manifest_for(scenario: str) -> ScenarioManifest:
    return load_manifest(SCENARIOS / scenario)


def make_request(scenario: str = "baseline-corroboration", **kw: Any) -> TaskRequest:
    manifest = manifest_for(scenario)
    task = next(t for t in manifest.template.tasks if t.agent.value == "sigint-agent")
    body: dict[str, Any] = {
        "task_id": "tsk_test1",
        "investigation_id": "inv_test1",
        "task_type": task.task_type,
        "objective": manifest.objective,
        "input_refs": list(task.input_refs),
        "scenario_id": scenario,
        "classification_ceiling": "DEMO-HIGH",
        "attempt": 1,
        "token_budget": 15000,
        "deadline": (datetime.now(UTC) + timedelta(seconds=180)).isoformat(),
        "contract_version": "1.0",
        "trace_context": {"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"},
    }
    body.update(kw)
    return TaskRequest.model_validate(body)


def valid_output(n: int, **override: Any) -> dict[str, Any]:
    return {
        "classifications": [
            {
                "candidate_id": f"C{i}",
                "pattern_label": "RECURRING_CONTACT",
                "confidence": 0.8,
                "statement": f"Repeated contacts observed for candidate {i}.",
                "reasoning_summary": "Several records on distinct days.",
            }
            | override
            for i in range(1, n + 1)
        ]
    }


class FakeProvider:
    """Deterministic test double at the provider boundary. Each call pops the next scripted
    response: a dict/str (validated like a real provider) or an exception instance."""

    def __init__(self, *responses: Any, usage: tuple[int, int] = (100, 50)) -> None:
        self.responses = list(responses)
        self.usage = usage
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        *,
        task: str,
        instructions: str,
        prompt_version: str,
        prompt_sha256: str,
        input_data: dict[str, Any],
        output_schema: type[BaseModel],
        budget: TokenBudget,
        call_ordinal: int,
        key_context: ReplayKeyContext,
    ) -> LLMResult:
        self.calls.append(
            {"input_data": input_data, "call_ordinal": call_ordinal, "key_context": key_context}
        )
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        usage = TaskUsage(input_tokens=self.usage[0], output_tokens=self.usage[1], llm_calls=1)
        try:
            out = parse_output(output_schema, item)
        except OutputParseError as exc:
            raise LLMOutputInvalidError(
                exc.reason, usage=usage, model_id="fake-model", raw_response=exc.raw
            ) from None
        return LLMResult(output=out, usage=usage, model_id="fake-model", latency_ms=1)


class ScenarioCopy:
    """A writable copy of a scenario (manifest + SIGINT fixtures) under tmp_path, so tests can
    tamper with fixtures or plant text and re-hash the manifest."""

    def __init__(self, tmp_path: Path, scenario: str) -> None:
        self.scenario = scenario
        self.scenarios = tmp_path / "scenarios"
        self.fixtures = tmp_path / "fixtures"
        (self.scenarios / scenario).mkdir(parents=True)
        shutil.copytree(FIXTURES / "sigint" / scenario, self.fixtures / "sigint" / scenario)
        self.raw = yaml.safe_load((SCENARIOS / scenario / "manifest.yaml").read_text())
        self.save()

    def fixture_path(self, evidence_id: str) -> Path:
        return self.fixtures / "sigint" / self.scenario / f"{evidence_id}.json"

    def edit_fixture(self, evidence_id: str, fn: Callable[[dict], None], rehash: bool) -> None:
        path = self.fixture_path(evidence_id)
        data = json.loads(path.read_text())
        fn(data)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        if rehash:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            for ev in self.raw["evidence"]:
                if ev["id"] == evidence_id:
                    ev["sha256"] = digest
            self.save()

    def save(self) -> None:
        (self.scenarios / self.scenario / "manifest.yaml").write_text(yaml.safe_dump(self.raw))

    @property
    def manifest(self) -> ScenarioManifest:
        return load_manifest(self.scenarios / self.scenario)


@pytest.fixture
def baseline_copy(tmp_path: Path) -> ScenarioCopy:
    return ScenarioCopy(tmp_path, "baseline-corroboration")
