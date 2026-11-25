"""Frontend/service HTTP API contracts (request/response bodies, RFC 7807 errors).

Submodules: ``errors``, ``states``, ``findings``, ``assessments``, ``retraction``,
``investigations``. Only dependency-free names are re-exported here to avoid import cycles
with ``nexus_contracts.a2a``.
"""

from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCategory, ErrorCode
from nexus_contracts.api.problem import FieldError, ProblemDetails
from nexus_contracts.api.states import InvestigationStatus, OrchestratorTaskState

__all__ = [
    "CATEGORY_BY_CODE",
    "ErrorCategory",
    "ErrorCode",
    "FieldError",
    "InvestigationStatus",
    "OrchestratorTaskState",
    "ProblemDetails",
]
