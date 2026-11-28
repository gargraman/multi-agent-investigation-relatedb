"""Read scope: what a read caller may see, resolved once per request (S4a FR-201..206).

``read_scope(op)`` wraps :func:`authorize` and turns the caller's policy clearance into a
:class:`ReadScope` that every read query is parameterised with (``$max_rank``, ``$trusted_only``).
The analysis-agent additionally reads *trusted* findings only and must name the task the read is
for (``X-Nexus-Task-Id``): the retrieval receipt is keyed on it (FR-204). Every other caller
ignores that header.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from pydantic import TypeAdapter, ValidationError

from nexus_context_service.auth import authorize
from nexus_context_service.errors import ProblemError
from nexus_context_service.policy import Policy
from nexus_context_service.queries.context import TRUSTED_STATUSES
from nexus_context_service.repository import AuditContext
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.common import TaskId
from nexus_shared.security import TASK_ID_HEADER

ANALYSIS_AGENT = "analysis-agent"

_TASK_ID = TypeAdapter(TaskId)


@dataclass(frozen=True)
class ReadScope:
    audit: AuditContext
    max_rank: int  # highest classification rank the caller may read
    trusted_only: bool  # True: only ACTIVE / DISPUTED findings are visible at all
    task_id: str | None  # receipt key; analysis-agent reads only

    def query_params(self) -> dict[str, Any]:
        """The parameters every scoped read query binds (``queries.context`` predicates)."""
        return {
            "max_rank": self.max_rank,
            "trusted_only": self.trusted_only,
            "trusted_statuses": list(TRUSTED_STATUSES),
        }


def read_scope(operation: str) -> Callable[[Request], Awaitable[ReadScope]]:
    """FastAPI dependency: ``authorize(operation)`` plus the caller's :class:`ReadScope`."""
    authorizer = authorize(operation)

    async def _dep(request: Request) -> ReadScope:
        audit = await authorizer(request)
        policy: Policy = request.app.state.policy
        max_rank = policy.read_rank(audit.caller)
        if audit.caller != ANALYSIS_AGENT:
            return ReadScope(audit=audit, max_rank=max_rank, trusted_only=False, task_id=None)
        raw = request.headers.get(TASK_ID_HEADER)
        try:
            task_id = _TASK_ID.validate_python(raw)
        except ValidationError as exc:
            raise ProblemError(
                400,
                f"{TASK_ID_HEADER} header is required and must be a task id",
                code=ErrorCode.SCHEMA_INVALID,
            ) from exc
        return ReadScope(audit=audit, max_rank=max_rank, trusted_only=True, task_id=task_id)

    return _dep
