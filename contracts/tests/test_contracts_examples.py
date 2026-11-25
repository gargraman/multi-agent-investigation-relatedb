# ruff: noqa: E501  (spec §12 examples are kept verbatim)
"""Round-trip the spec §12 JSON examples through the contract models.

The examples use truncated ids (``tsk_01JD3K8...``) and a ``<configured-model>`` placeholder.
Ids are made concrete by replacing ``...`` with a fixed ULID tail; everything else is verbatim.
"""

import json
from typing import Any

from nexus_contracts.a2a import TaskRequest, TaskResult
from nexus_contracts.api.assessments import Assessment

TAIL = "ABCDEFGH"

TASK_REQUEST = """{
  "task_id": "tsk_01JD3K8...",
  "investigation_id": "inv_01JD3K7...",
  "task_type": "analyze_synthetic_communications",
  "objective": "Identify recurring contact patterns in the supplied synthetic metadata.",
  "input_refs": ["ev-sig-001", "ev-sig-002"],
  "scenario_id": "baseline-corroboration",
  "classification_ceiling": "DEMO-MID",
  "attempt": 1,
  "token_budget": 20000,
  "deadline": "2026-11-24T10:03:00Z",
  "contract_version": "1.0",
  "trace_context": {"traceparent": "00-...", "tracestate": ""}
}"""

TASK_RESULT = """{
  "task_id": "tsk_01JD3K8...",
  "agent_id": "sigint-agent",
  "status": "COMPLETED",
  "summary": "A recurring synthetic contact pattern was observed between two fictional handles.",
  "finding_ids": ["fnd_01JD3K9..."],
  "warnings": [],
  "attempt": 1,
  "usage": {"input_tokens": 3120, "output_tokens": 640, "llm_calls": 1},
  "model_id": "<configured-model>",
  "prompt_version": "sigint-extract@1.2.0",
  "llm_mode": "live",
  "completed_at": "2026-11-24T09:59:20Z",
  "error": null
}"""

ASSESSMENT = """{
  "assessment_id": "asm_01JD3KA...",
  "investigation_id": "inv_01JD3K7...",
  "summary": "The synthetic observations suggest a possible association between ENT-ALPHA and ENT-BRAVO.",
  "conclusions": [{
    "statement": "ENT-ALPHA and ENT-BRAVO may be coordinating activity near LOC-GRID-14.",
    "kind": "INFERENCE",
    "confidence": 0.62,
    "confidence_band": "MODERATE",
    "supporting_finding_ids": ["fnd_sig_001", "fnd_geo_002"],
    "limitations": ["Co-location does not independently establish coordination."]
  }],
  "contradictions": [{
    "finding_ids": ["fnd_geo_002", "fnd_geo_005"],
    "description": "Two observations place ENT-ALPHA in different grid cells in overlapping windows.",
    "effect_on_confidence": "Lowered from HIGH to MODERATE."
  }],
  "information_gaps": ["No independent corroboration of ENT-BRAVO's communications."],
  "status": "ACTIVE",
  "model_id": "<configured-model>",
  "prompt_version": "analysis-assess@1.0.0"
}"""


def _concrete(raw: str) -> dict[str, Any]:
    # Only id fields carry "..."; the traceparent placeholder is left as-is.
    data = json.loads(raw.replace('..."', f'{TAIL}"').replace(f'"00-{TAIL}"', '"00-..."'))
    return data


def _assert_round_trip(model: type, raw: str, *, added_defaults: dict[str, Any]) -> None:
    data = _concrete(raw)
    parsed = model.model_validate(data)
    dumped = json.loads(parsed.model_dump_json())
    expected = {**data, **added_defaults}
    assert dumped == expected
    assert model.model_validate_json(parsed.model_dump_json()) == parsed


def test_task_request_example_round_trips() -> None:
    _assert_round_trip(TaskRequest, TASK_REQUEST, added_defaults={})


def test_task_result_example_round_trips() -> None:
    data = _concrete(TASK_RESULT)
    usage = {**data["usage"], "replayed": False}  # ADR-006 E7 field, defaulted
    _assert_round_trip(
        TaskResult, TASK_RESULT, added_defaults={"usage": usage, "assessment_id": None}
    )


def test_assessment_example_round_trips() -> None:
    _assert_round_trip(Assessment, ASSESSMENT, added_defaults={"follow_up_questions": []})


def test_task_error_example_shape() -> None:
    data = _concrete(TASK_RESULT)
    data.update(status="FAILED", finding_ids=[], completed_at=None)
    data["error"] = {"category": "llm", "code": "schema_invalid"}  # §12.2 example
    assert TaskResult.model_validate(data).error.code == "schema_invalid"
