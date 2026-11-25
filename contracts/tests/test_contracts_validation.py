"""Local validation rules of the S0 contract models (spec §8.2, §9.3, §12; ADR-007 G3/G7/G10)."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from nexus_contracts.a2a import AgentCard, TaskError, TaskRequest, TaskResult
from nexus_contracts.api.assessments import AssessmentSubmission, ProposedEdge
from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCode
from nexus_contracts.api.findings import (
    FindingBatchRequest,
    FindingDraft,
    LinkedToAssertion,
    PlacedAtAssertion,
    canonical_link_pair,
)
from nexus_contracts.api.investigations import InvestigationCreateRequest
from nexus_contracts.events import TimelineEvent

T0 = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)
H = timedelta(hours=1)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _task_request(**over: Any) -> dict[str, Any]:
    base = {
        "task_id": "tsk_01",
        "investigation_id": "inv_01",
        "task_type": "analyze_synthetic_communications",
        "objective": "x",
        "input_refs": ["ev-sig-001"],
        "scenario_id": "baseline-corroboration",
        "classification_ceiling": "DEMO-MID",
        "attempt": 1,
        "token_budget": 1000,
        "deadline": "2026-11-24T10:03:00Z",
        "contract_version": "1.0",
        "trace_context": {"traceparent": "00-abc"},
    }
    return {**base, **over}


def _link(subject: str = "hA", obj: str = "hC", **over: Any) -> dict[str, Any]:
    return {
        "type": "LINKED_TO",
        "subject": subject,
        "object": obj,
        "link_type": "CONTACT",
        "valid_from": _iso(T0),
        "valid_until": _iso(T0 + H),
        **over,
    }


def _placed(**over: Any) -> dict[str, Any]:
    return {
        "type": "PLACED_AT",
        "subject": "C",
        "location": "LOC-1",
        "valid_from": _iso(T0),
        "valid_until": _iso(T0 + H),
        **over,
    }


def _finding(**over: Any) -> dict[str, Any]:
    base = {
        "kind": "OBSERVATION",
        "statement": "s",
        "confidence": 0.7,
        "confidence_band": "MODERATE",
        "classification": "DEMO-LOW",
        "valid_from": _iso(T0 - H),
        "valid_until": _iso(T0 + 2 * H),
        "about": ["hA", "hC"],
        "evidence": [
            {
                "evidence_id": "ev-sig-001",
                "sha256": "0" * 64,
                "owner_node_id": "sigint-agent",
                "summary": "comms log",
            }
        ],
        "assertions": [_link()],
        "reasoning_summary": "r",
        "model_id": "m",
        "prompt_version": "p@1",
        "schema_version": "1",
    }
    return {**base, **over}


# --- strictness & datetimes -------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,data",
    [
        (TaskRequest, _task_request(taks_type="typo")),
        (FindingDraft, _finding(confidnce=0.1)),
        (InvestigationCreateRequest, {"scenario_id": "s", "objectiv": "x"}),
    ],
)
def test_unknown_fields_rejected(model: type, data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        model.model_validate(data)


def test_task_request_is_frozen() -> None:
    req = TaskRequest.model_validate(_task_request())
    with pytest.raises(ValidationError):
        req.attempt = 2  # type: ignore[misc]


@pytest.mark.parametrize("deadline", ["2026-11-24T10:03:00", "2026-11-24T10:03:00+02:00"])
def test_naive_or_non_utc_datetime_rejected(deadline: str) -> None:
    with pytest.raises(ValidationError):
        TaskRequest.model_validate(_task_request(deadline=deadline))


def test_explicit_zero_offset_accepted() -> None:
    req = TaskRequest.model_validate(_task_request(deadline="2026-11-24T10:03:00+00:00"))
    assert req.deadline.utcoffset() == timedelta(0)
    other = datetime(2026, 11, 24, tzinfo=timezone(timedelta(hours=1)))
    with pytest.raises(ValidationError):
        TaskRequest.model_validate(_task_request(deadline=other))


# --- attempt ----------------------------------------------------------------------------------


@pytest.mark.parametrize("attempt", [0, -1])
def test_attempt_out_of_range_rejected(attempt: int) -> None:
    with pytest.raises(ValidationError):
        TaskRequest.model_validate(_task_request(attempt=attempt))
    with pytest.raises(ValidationError):
        FindingBatchRequest.model_validate(
            {
                "task_id": "tsk_1",
                "investigation_id": "inv_1",
                "attempt": attempt,
                "findings": [_finding()],
            }
        )


def test_ids_need_spec_prefix() -> None:
    with pytest.raises(ValidationError):
        TaskRequest.model_validate(_task_request(task_id="task-01"))


def test_unknown_task_type_is_not_a_schema_error() -> None:
    # Must reach the agent so it can answer REJECTED (§8.2), not 422.
    assert TaskRequest.model_validate(_task_request(task_type="something_new")).task_type


# --- windows & assertions (ADR-007 G7/G10) ----------------------------------------------------


@pytest.mark.parametrize("until", [T0, T0 - H])
def test_window_must_be_half_open_non_empty(until: datetime) -> None:
    with pytest.raises(ValidationError, match="valid_from < valid_until"):
        PlacedAtAssertion.model_validate(_placed(valid_until=_iso(until)))
    with pytest.raises(ValidationError, match="valid_from < valid_until"):
        FindingDraft.model_validate(
            _finding(valid_from=_iso(T0), valid_until=_iso(until), assertions=[])
        )


def test_assertion_window_must_lie_inside_finding_window() -> None:
    late = _link(valid_until=_iso(T0 + 3 * H))
    with pytest.raises(ValidationError, match="inside the finding window"):
        FindingDraft.model_validate(_finding(assertions=[late]))
    early = _link(valid_from=_iso(T0 - 2 * H))
    with pytest.raises(ValidationError, match="inside the finding window"):
        FindingDraft.model_validate(_finding(assertions=[early]))


def test_assertion_window_may_touch_finding_bounds() -> None:
    exact = _link(valid_from=_iso(T0 - H), valid_until=_iso(T0 + 2 * H))
    assert FindingDraft.model_validate(_finding(assertions=[exact]))


def test_subject_must_differ_from_object() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        LinkedToAssertion.model_validate(_link("hA", "hA"))


def test_subject_must_be_in_about() -> None:
    with pytest.raises(ValidationError, match="not in about"):
        FindingDraft.model_validate(_finding(about=["hC", "hX"]))
    with pytest.raises(ValidationError, match="not in about"):
        FindingDraft.model_validate(_finding(about=["hA"], assertions=[_placed()]))


def test_assertion_union_discriminates_on_type() -> None:
    f = FindingDraft.model_validate(
        _finding(about=["hA", "hC", "C"], assertions=[_link(), _placed()])
    )
    assert isinstance(f.assertions[0], LinkedToAssertion)
    assert isinstance(f.assertions[1], PlacedAtAssertion)
    with pytest.raises(ValidationError):
        FindingDraft.model_validate(_finding(assertions=[{**_link(), "type": "OBSERVED_AT"}]))
    with pytest.raises(ValidationError):  # PLACED_AT has no object
        FindingDraft.model_validate(_finding(assertions=[{**_placed(), "object": "hA"}]))


def test_contact_is_normalised_to_canonical_order() -> None:
    a = LinkedToAssertion.model_validate(_link("hC", "hA"))
    assert (a.subject, a.object) == ("hA", "hC")
    assert canonical_link_pair("hC", "hA") == ("hA", "hC")


def test_content_hash_independent_of_pair_and_assertion_order() -> None:
    a1 = _link("hA", "hC")
    a2 = _link("hC", "hA", valid_from=_iso(T0 + H / 2))
    batch = {"task_id": "tsk_1", "investigation_id": "inv_1", "attempt": 1}
    h1 = FindingBatchRequest.model_validate({**batch, "findings": [_finding(assertions=[a1, a2])]})
    h2 = FindingBatchRequest.model_validate(
        {**batch, "attempt": 2, "findings": [_finding(assertions=[a2, _link("hC", "hA")])]}
    )
    assert h1.content_hash() == h2.content_hash()
    h3 = FindingBatchRequest.model_validate({**batch, "findings": [_finding(confidence=0.71)]})
    assert h3.content_hash() != h1.content_hash()


def test_finding_needs_evidence_and_about() -> None:
    with pytest.raises(ValidationError):
        FindingDraft.model_validate(_finding(evidence=[]))
    with pytest.raises(ValidationError):
        FindingDraft.model_validate(_finding(about=[], assertions=[]))
    with pytest.raises(ValidationError):
        FindingDraft.model_validate(_finding(confidence=1.2))


# --- task result rules ------------------------------------------------------------------------


def _result(**over: Any) -> dict[str, Any]:
    base = {
        "task_id": "tsk_1",
        "agent_id": "sigint-agent",
        "status": "WORKING",
        "attempt": 1,
        "llm_mode": "replay",
    }
    return {**base, **over}


def test_failed_requires_error_and_completed_forbids_it() -> None:
    with pytest.raises(ValidationError, match="FAILED requires error"):
        TaskResult.model_validate(_result(status="FAILED"))
    err = {"category": "llm", "code": "schema_invalid"}
    with pytest.raises(ValidationError, match="only allowed"):
        TaskResult.model_validate(_result(status="COMPLETED", error=err))


def test_finding_ids_only_on_completed() -> None:
    with pytest.raises(ValidationError, match="only reported on COMPLETED"):
        TaskResult.model_validate(_result(finding_ids=["fnd_1"]))


def test_error_category_must_match_code() -> None:
    with pytest.raises(ValidationError, match="belongs to category"):
        TaskError.model_validate({"category": "budget", "code": "schema_invalid"})
    assert TaskError.for_code(ErrorCode.BUDGET_EXCEEDED).category == "budget"


def test_every_error_code_has_a_category() -> None:
    assert set(CATEGORY_BY_CODE) == set(ErrorCode)


def test_agent_card_requires_llm_mode_and_clearance() -> None:
    card = {
        "id": "geoint-agent",
        "role": "GEOINT",
        "task_types": ["t"],
        "version": "1",
        "clearance": "DEMO-MID",
    }
    with pytest.raises(ValidationError):
        AgentCard.model_validate(card)
    assert AgentCard.model_validate({**card, "llm_mode": "replay"}).llm_mode == "replay"


# --- assessments ------------------------------------------------------------------------------


def test_edge_self_loop_rejected() -> None:
    with pytest.raises(ValidationError, match="two different findings"):
        ProposedEdge(type="CONTRADICTS", source_finding_id="fnd_1", target_finding_id="fnd_1")


def test_assessment_submission_contradiction_pair_must_be_distinct() -> None:
    body = {
        "task_id": "tsk_a",
        "investigation_id": "inv_1",
        "attempt": 1,
        "schema_version": "1",
        "summary": "s",
        "model_id": "m",
        "prompt_version": "p",
        "conclusions": [
            {
                "statement": "c",
                "kind": "INFERENCE",
                "confidence": 0.5,
                "confidence_band": "MODERATE",
                "supporting_finding_ids": ["fnd_1"],
            }
        ],
        "contradictions": [
            {"finding_ids": ["fnd_1", "fnd_1"], "description": "d", "effect_on_confidence": "e"}
        ],
    }
    with pytest.raises(ValidationError, match="duplicates"):
        AssessmentSubmission.model_validate(body)
    body["contradictions"] = []
    body["proposed_edges"] = [
        {"type": "CORROBORATES", "source_finding_id": "fnd_1", "target_finding_id": "fnd_2"}
    ]
    assert AssessmentSubmission.model_validate(body).proposed_edges[0].type == "CORROBORATES"


# --- timeline ---------------------------------------------------------------------------------


def test_timeline_typed_payload_validated() -> None:
    ev = {
        "seq": 1,
        "investigation_id": "inv_1",
        "type": "investigation.status_changed",
        "payload": {"to_status": "RUNNING"},
        "created_at": _iso(T0),
    }
    assert TimelineEvent.model_validate(ev).typed_payload().to_status == "RUNNING"
    with pytest.raises(ValidationError):
        TimelineEvent.model_validate({**ev, "payload": {"to_status": "DONE"}})
    with pytest.raises(ValidationError):
        TimelineEvent.model_validate({**ev, "seq": 0})


def test_content_hash_independent_of_finding_and_list_order() -> None:
    batch = {"task_id": "tsk_1", "investigation_id": "inv_1", "attempt": 1}
    f1 = _finding(about=["hA", "hC"])
    f2 = _finding(about=["hC", "hA"], confidence=0.5)
    f1_reordered = _finding(about=["hC", "hA"])
    h1 = FindingBatchRequest.model_validate({**batch, "findings": [f1, f2]})
    h2 = FindingBatchRequest.model_validate({**batch, "findings": [f2, f1_reordered]})
    assert h1.content_hash() == h2.content_hash()


def test_content_hash_rounds_confidence_to_4dp() -> None:
    batch = {"task_id": "tsk_1", "investigation_id": "inv_1", "attempt": 1}
    h1 = FindingBatchRequest.model_validate({**batch, "findings": [_finding(confidence=0.7)]})
    h2 = FindingBatchRequest.model_validate(
        {**batch, "findings": [_finding(confidence=0.70000001)]}
    )
    assert h1.content_hash() == h2.content_hash()


def test_attempt_has_no_upper_bound_in_contract() -> None:
    # The two-attempt limit is engine policy (§8.2), not a wire rule.
    req = FindingBatchRequest.model_validate(
        {"task_id": "tsk_1", "investigation_id": "inv_1", "attempt": 3, "findings": [_finding()]}
    )
    assert req.attempt == 3
