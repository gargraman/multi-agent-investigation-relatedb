"""S2 contract deltas (specs/02-features/S2-sigint-agent.md §8.1)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nexus_contracts.a2a.task import AgentTaskStatus, TaskError, TaskSubmitResponse
from nexus_contracts.api.context import InvestigationContext
from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCategory, ErrorCode
from nexus_contracts.api.findings import AlreadyCommittedProblem
from nexus_contracts.api.problem import ProblemDetails
from nexus_contracts.events.timeline import InvestigationStatusChanged
from nexus_contracts.scenario.oracle import Oracle

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (ErrorCode.AGENT_NOT_ENABLED, ErrorCategory.POLICY),
        (ErrorCode.CONTEXT_UNAVAILABLE, ErrorCategory.LIFECYCLE),
        (ErrorCode.SUBMISSION_REJECTED, ErrorCategory.VALIDATION),
    ],
)
def test_new_error_codes_have_categories(code: ErrorCode, category: ErrorCategory) -> None:
    assert CATEGORY_BY_CODE[code] is category
    assert TaskError.for_code(code).category is category


@pytest.mark.parametrize("status", list(AgentTaskStatus))
def test_submit_response_accepts_any_current_status(status: AgentTaskStatus) -> None:
    """A duplicate submit returns the task's current status (G2)."""
    assert TaskSubmitResponse(task_id="tsk_abc", status=status).status is status


def test_already_committed_problem_round_trips_through_generic_problem() -> None:
    p = AlreadyCommittedProblem(title="Already committed", finding_ids=["fnd_1", "fnd_2"])
    body = p.model_dump(mode="json")
    assert body["status"] == 409 and body["code"] == "already_committed"
    # A client parsing with the generic model keeps the extension member (extra="allow").
    generic = ProblemDetails.model_validate(body)
    assert generic.model_extra == {"finding_ids": ["fnd_1", "fnd_2"]}
    assert AlreadyCommittedProblem.model_validate(body) == p


def test_already_committed_problem_requires_ids_and_409() -> None:
    with pytest.raises(ValidationError):
        AlreadyCommittedProblem(title="x", finding_ids=[])
    with pytest.raises(ValidationError):
        AlreadyCommittedProblem(title="x", status=422, finding_ids=["fnd_1"])


def test_status_changed_lists_skipped_agents() -> None:
    e = InvestigationStatusChanged(
        from_status="CREATED",
        to_status="RUNNING",
        skipped_agents=["geoint-agent", "analysis-agent"],
    )
    assert e.skipped_agents == ["geoint-agent", "analysis-agent"]
    assert InvestigationStatusChanged(to_status="CREATED").skipped_agents == []
    with pytest.raises(ValidationError):
        InvestigationStatusChanged(to_status="RUNNING", skipped_agents=["not-an-agent"])


def test_oracle_forbidden_strings_validated() -> None:
    assert Oracle(forbidden_output_strings=["EXFIL-TOKEN-7731"]).forbidden_output_strings
    with pytest.raises(ValidationError):
        Oracle(forbidden_output_strings=["short"])
    with pytest.raises(ValidationError):
        Oracle(forbidden_output_strings=["EXFIL-TOKEN-7731", "EXFIL-TOKEN-7731"])


def _finding(**over: object) -> dict:
    base = {
        "id": "fnd_1",
        "kind": "OBSERVATION",
        "status": "ACTIVE",
        "statement": "Recurring contact between two handles.",
        "confidence": 0.8,
        "confidence_band": "HIGH",
        "classification": "DEMO-LOW",
        "source_node_id": "sigint-agent",
        "task_id": "tsk_1",
        "attempt": 1,
        "model_id": "gpt-5.4-mini-2026-03-17",
        "prompt_version": "sigint-classify@1.0.0",
        "valid_from": T0.isoformat(),
        "valid_until": T1.isoformat(),
        "about": ["HDL-ALPHA-1", "HDL-CHARLIE-1"],
        "evidence": [{"evidence_id": "ev-sig-004", "sha256": "a" * 64, "summary": "2 contacts"}],
        "assertions": [
            {
                "type": "LINKED_TO",
                "link_type": "CONTACT",
                "subject": "HDL-ALPHA-1",
                "object": "HDL-CHARLIE-1",
                "valid_from": T0.isoformat(),
                "valid_until": T1.isoformat(),
            }
        ],
        "created_at": T1.isoformat(),
    }
    return base | over


def test_investigation_context_parses_a_committed_finding() -> None:
    ctx = InvestigationContext.model_validate(
        {"investigation_id": "inv_1", "status": "RUNNING", "findings": [_finding()]}
    )
    assert ctx.findings[0].assertions[0].type == "LINKED_TO"
    with pytest.raises(ValidationError):
        InvestigationContext.model_validate(
            {"investigation_id": "inv_1", "status": "RUNNING", "findings": [_finding(evidence=[])]}
        )
