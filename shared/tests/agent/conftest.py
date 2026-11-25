"""Fixtures for the shared agent-runtime tests. Agent-neutral: no node package is imported."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nexus_contracts.a2a.task import TaskRequest
from nexus_contracts.api.findings import FindingBatchRequest
from nexus_contracts.scenario.loader import load_manifest

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = ROOT / "data/scenarios"


def make_request(scenario: str = "baseline-corroboration", **kw: Any) -> TaskRequest:
    manifest = load_manifest(SCENARIOS / scenario)
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


def make_batch() -> FindingBatchRequest:
    """A minimal valid findings batch (the client under test treats the body as opaque)."""
    return FindingBatchRequest.model_validate(
        {
            "task_id": "tsk_test1",
            "investigation_id": "inv_test1",
            "attempt": 1,
            "findings": [
                {
                    "kind": "INFERENCE",
                    "statement": "Repeated contacts observed.",
                    "confidence": 0.8,
                    "confidence_band": "HIGH",
                    "classification": "DEMO-HIGH",
                    "about": ["handle-a", "handle-b"],
                    "evidence": [
                        {
                            "evidence_id": "ev-1",
                            "sha256": "a" * 64,
                            "owner_node_id": "sigint-agent",
                            "summary": "Contact records.",
                        }
                    ],
                    "assertions": [
                        {
                            "type": "LINKED_TO",
                            "subject": "handle-a",
                            "object": "handle-b",
                            "link_type": "CONTACT",
                            "valid_from": "2026-01-01T00:00:00Z",
                            "valid_until": "2026-01-02T00:00:00Z",
                        }
                    ],
                    "reasoning_summary": "Several records on distinct days.",
                    "model_id": "fake-model",
                    "prompt_version": "test-v1",
                    "schema_version": "1",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "valid_until": "2026-01-02T00:00:00Z",
                }
            ],
        }
    )
