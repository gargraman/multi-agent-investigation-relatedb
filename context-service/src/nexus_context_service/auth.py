"""Caller authentication and the operation allowlist (spec §14.1, ADR-005 D4).

The allowlist is ``policy.yaml`` (see :mod:`nexus_context_service.policy`), held on
``app.state.policy``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Request

from nexus_context_service.errors import ProblemError
from nexus_context_service.lane import LaneUnavailable
from nexus_context_service.policy import Policy
from nexus_context_service.repository import AuditContext, GraphRepository
from nexus_shared.security import ACTOR_HEADER, TokenRegistry, resolve_actor

log = logging.getLogger(__name__)


def authorize(operation: str) -> Callable[[Request], Awaitable[AuditContext]]:
    """FastAPI dependency: 401 without a valid bearer, 403 (audited) for a disallowed caller."""

    async def _dep(request: Request) -> AuditContext:
        registry: TokenRegistry = request.app.state.token_registry
        caller = registry.authenticate(request.headers.get("Authorization"))
        if caller is None:
            raise ProblemError(
                401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
            )
        audit = AuditContext(
            caller=caller, actor=resolve_actor(caller, request.headers.get(ACTOR_HEADER))
        )
        policy: Policy = request.app.state.policy
        if not policy.allows(caller, operation):
            repo: GraphRepository = request.app.state.repository
            try:  # standalone denial audit (spec §14.4, ADR-002)
                await repo.record_denial(audit, operation, request.path_params.get("id"))
            except LaneUnavailable:
                log.warning("denial not audited: store not ready", extra={"op": operation})
            raise ProblemError(403, f"caller is not allowed to perform {operation}")
        return audit

    return _dep
