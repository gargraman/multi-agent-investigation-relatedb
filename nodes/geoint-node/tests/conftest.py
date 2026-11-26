from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from nexus_contracts.a2a.task import TaskRequest, TaskUsage
from nexus_contracts.scenario.fixtures import SightingLog
from nexus_contracts.scenario.loader import load_manifest
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_shared.llm import LLMOutputInvalidError, LLMResult, ReplayKeyContext, TokenBudget
from nexus_shared.llm.schema import OutputParseError, parse_output

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = ROOT / "data/scenarios"
AGENT_MANIFESTS = ROOT / "data/agent-manifests"  # what the agents mount: no oracle
FIXTURES = ROOT / "data/fixtures"


def manifest_for(scenario: str) -> ScenarioManifest:
    return load_manifest(SCENARIOS / scenario)


def load_logs(scenario: str) -> dict[str, SightingLog]:
    """Every GEOINT sighting log of a scenario, keyed by evidence id."""
    logs = {}
    for path in sorted((FIXTURES / "geoint" / scenario).glob("ev-geo-*.json")):
        log = SightingLog.model_validate_json(path.read_text())
        logs[log.evidence_id] = log
    return logs


def make_request(scenario: str = "baseline-corroboration", **kw: Any) -> TaskRequest:
    manifest = manifest_for(scenario)
    task = next(t for t in manifest.template.tasks if t.agent.value == "geoint-agent")
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


def valid_output_for(input_data: dict[str, Any], **override: Any) -> dict[str, Any]:
    """A clean, schema-valid output labelling every candidate in the provider input."""
    return {
        "classifications": [
            {
                "candidate_id": c["candidate_id"],
                "pattern_label": "CO_LOCATION",
                "confidence": 0.8,
                "statement": f"Subjects observed together at {c['location']}.",
                "reasoning_summary": "Overlapping sightings in one grid cell.",
            }
            | override
            for c in input_data["candidates"]
        ]
    }


class ScriptedProvider:
    """Deterministic test double at the provider boundary. Each response is a callable of the
    provider input (e.g. :func:`valid_output_for`), a dict/str validated like a real provider's
    output, or an exception instance. The last response repeats when the script runs out."""

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
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(input_data)
        usage = TaskUsage(input_tokens=self.usage[0], output_tokens=self.usage[1], llm_calls=1)
        try:
            out = parse_output(output_schema, item)
        except OutputParseError as exc:
            raise LLMOutputInvalidError(
                exc.reason, usage=usage, model_id="fake-model", raw_response=exc.raw
            ) from None
        return LLMResult(output=out, usage=usage, model_id="fake-model", latency_ms=1)


class ScenarioCopy:
    """A writable copy of a scenario (manifest + GEOINT fixtures) under tmp_path."""

    def __init__(self, tmp_path: Path, scenario: str) -> None:
        self.scenario = scenario
        self.scenarios = tmp_path / "scenarios"
        self.fixtures = tmp_path / "fixtures"
        (self.scenarios / scenario).mkdir(parents=True)
        shutil.copytree(FIXTURES / "geoint" / scenario, self.fixtures / "geoint" / scenario)
        self.raw = yaml.safe_load((SCENARIOS / scenario / "manifest.yaml").read_text())
        self.save()

    def fixture_path(self, evidence_id: str) -> Path:
        return self.fixtures / "geoint" / self.scenario / f"{evidence_id}.json"

    def edit_fixture(
        self, evidence_id: str, fn: Callable[[dict], None], rehash: bool = False
    ) -> None:
        """Edit a fixture; without ``rehash`` the manifest sha256 no longer matches."""
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
