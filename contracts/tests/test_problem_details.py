import pytest
from pydantic import ValidationError

from nexus_contracts.api import FieldError, ProblemDetails


def test_defaults_type_to_about_blank() -> None:
    problem = ProblemDetails(title="Not Found", status=404)
    assert problem.type == "about:blank"
    assert problem.code is None


def test_rejects_out_of_range_status() -> None:
    with pytest.raises(ValidationError):
        ProblemDetails(title="Bad", status=700)


def test_preserves_unknown_extension_members() -> None:
    # RFC 7807 §3.2: problem types may add extension members; clients must not break on them.
    problem = ProblemDetails.model_validate({"title": "Bad", "status": 400, "retry_after_s": 5})
    assert problem.model_dump()["retry_after_s"] == 5


def test_carries_field_errors_for_422() -> None:
    problem = ProblemDetails(
        title="Unprocessable Entity",
        status=422,
        code="validation_failed",
        errors=[FieldError(loc=["body", "objective"], msg="Field required", type="missing")],
    )
    dumped = problem.model_dump(exclude_none=True)
    assert dumped["errors"] == [
        {"loc": ["body", "objective"], "msg": "Field required", "type": "missing"}
    ]


def test_round_trips_json() -> None:
    raw = (
        '{"type":"about:blank","title":"Locked","status":422,'
        '"code":"replay_objective_locked","errors":[{"loc":["body","objective"],'
        '"msg":"locked in replay","type":"value_error"}]}'
    )
    assert ProblemDetails.model_validate_json(raw).code == "replay_objective_locked"
