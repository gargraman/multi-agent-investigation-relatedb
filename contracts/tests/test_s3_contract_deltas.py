"""S3 contract deltas (S3a WP-A1): error codes, cancel responses, task event attempt/deadline."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nexus_contracts.a2a.status_mapping import REDISPATCHABLE_CODES
from nexus_contracts.a2a.task import AgentTaskStatus, TaskCancelResponse
from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCategory, ErrorCode
from nexus_contracts.api.investigations import InvestigationCancelResponse
from nexus_contracts.events.timeline import TaskStatusChanged
from nexus_contracts.registry import SCHEMA_MODELS

DEADLINE = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)


def test_internal_error_is_lifecycle_and_not_redispatchable() -> None:
    assert ErrorCode("internal_error") is ErrorCode.INTERNAL_ERROR
    assert CATEGORY_BY_CODE[ErrorCode.INTERNAL_ERROR] is ErrorCategory.LIFECYCLE
    assert ErrorCode.INTERNAL_ERROR not in REDISPATCHABLE_CODES


def test_new_codes_have_categories() -> None:
    assert ErrorCode("investigation_terminal") is ErrorCode.INVESTIGATION_TERMINAL
    assert ErrorCode("forbidden") is ErrorCode.FORBIDDEN
    assert CATEGORY_BY_CODE[ErrorCode.FORBIDDEN] is ErrorCategory.POLICY
    assert CATEGORY_BY_CODE[ErrorCode.INVESTIGATION_TERMINAL] is ErrorCategory.CONFLICT


def test_cancel_responses_round_trip() -> None:
    r = TaskCancelResponse(task_id="tsk_1", status=AgentTaskStatus.CANCELED)
    assert TaskCancelResponse.model_validate(r.model_dump(mode="json")) == r
    i = InvestigationCancelResponse(id="inv_1", status="CANCELLED", context_closed=False)
    assert i.context_closed is False
    assert InvestigationCancelResponse.model_validate(i.model_dump(mode="json")) == i


def _payload(**extra: object) -> dict[str, object]:
    return {
        "task_id": "tsk_1",
        "agent_id": "geoint-agent",
        "to_state": "RUNNING",
        **extra,
    }


def test_task_status_changed_payload_carries_attempt_and_optional_deadline() -> None:
    p = TaskStatusChanged.model_validate(_payload(attempt=2, deadline=DEADLINE.isoformat()))
    assert p.attempt == 2
    assert p.deadline == DEADLINE
    assert TaskStatusChanged.model_validate(_payload(attempt=1)).deadline is None
    with pytest.raises(ValidationError):
        TaskStatusChanged.model_validate(_payload())


def test_new_models_registered() -> None:
    assert {"TaskCancelResponse", "InvestigationCancelResponse"} <= {
        m.__name__ for m in SCHEMA_MODELS.values()
    }
    assert {"task-cancel-response", "investigation-cancel-response"} <= set(SCHEMA_MODELS)
