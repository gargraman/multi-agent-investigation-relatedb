"""Frontend/service HTTP API contracts (request/response bodies, RFC 7807 errors)."""

from nexus_contracts.api.problem import FieldError, ProblemDetails

__all__ = ["FieldError", "ProblemDetails"]
