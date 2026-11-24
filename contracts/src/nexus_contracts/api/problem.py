"""RFC 7807 problem details (``application/problem+json``).

Every Nexus service returns errors in this shape (spec §12). Nexus extension members:

- ``code``: a stable, machine-readable error code (e.g. ``replay_objective_locked``).
- ``errors``: per-field validation failures, set on ``422`` responses.

Unknown members are accepted and preserved, as RFC 7807 §3.2 allows, so a service can add an
extension member without breaking clients that parse problems with this model.
"""

from pydantic import BaseModel, ConfigDict, Field

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"


class FieldError(BaseModel):
    """One validation failure, located by a JSON-pointer-like path."""

    model_config = ConfigDict(extra="allow", frozen=True)

    loc: list[str | int] = Field(
        description="Path to the offending value, e.g. ['body', 'objective']."
    )
    msg: str = Field(description="Human-readable reason.")
    type: str = Field(description="Machine-readable error type, e.g. 'missing'.")


class ProblemDetails(BaseModel):
    """An RFC 7807 problem details object."""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: str = Field(
        default="about:blank",
        description="URI reference identifying the problem type.",
    )
    title: str = Field(description="Short, human-readable summary of the problem type.")
    status: int = Field(ge=100, le=599, description="HTTP status code for this occurrence.")
    detail: str | None = Field(
        default=None,
        description="Human-readable explanation specific to this occurrence.",
    )
    instance: str | None = Field(
        default=None,
        description="URI reference identifying this specific occurrence.",
    )
    code: str | None = Field(
        default=None,
        description="Nexus extension: stable machine-readable error code.",
    )
    errors: list[FieldError] | None = Field(
        default=None,
        description="Nexus extension: per-field validation failures (422 responses).",
    )
