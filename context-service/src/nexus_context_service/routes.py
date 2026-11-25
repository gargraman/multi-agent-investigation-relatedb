"""HTTP routes (spec §12.3). S1: investigation create/close + health probes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse

from nexus_context_service.auth import authorize
from nexus_context_service.errors import ProblemError
from nexus_context_service.repository import (
    AuditContext,
    GraphRepository,
    InvestigationClosed,
    InvestigationContentConflict,
    InvestigationNotFound,
)
from nexus_contracts.api.context import (
    ContextInvestigationClose,
    ContextInvestigationClosed,
    ContextInvestigationCreate,
    ContextInvestigationCreated,
)
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.problem import FieldError
from nexus_contracts.common import EntityType

router = APIRouter()

INVESTIGATION_ID_PATTERN = r"^inv_[A-Za-z0-9_-]{1,64}$"


def _repo(request: Request) -> GraphRepository:
    return request.app.state.repository


def _require_ready(request: Request) -> None:
    if not request.app.state.ready:
        raise ProblemError(503, "graph store is not ready")


def _semantic_errors(body: ContextInvestigationCreate) -> list[FieldError]:
    """Cross-field checks the schema can't express; caught before any write (no half-writes)."""
    errors: list[FieldError] = []
    types: dict[str, EntityType] = {}
    for i, e in enumerate(body.entities):
        if e.manifest_id in types:
            errors.append(_fe(["body", "entities", i, "manifest_id"], "duplicate manifest_id"))
        types[e.manifest_id] = e.type
        if e.type is EntityType.LOCATION and e.position is None:
            errors.append(_fe(["body", "entities", i, "position"], "LOCATION needs a position"))
    seen_ev: set[str] = set()
    for i, ev in enumerate(body.evidence):
        if ev.manifest_id in seen_ev:
            errors.append(_fe(["body", "evidence", i, "manifest_id"], "duplicate manifest_id"))
        seen_ev.add(ev.manifest_id)
    for i, r in enumerate(body.relationships):
        # P0 manifest edges are USED_BY only: HANDLE -> PERSON (spec §9.3, ADR-007).
        if types.get(r.source) is not EntityType.HANDLE:
            errors.append(_fe(["body", "relationships", i, "source"], "must be a HANDLE entity"))
        if types.get(r.target) is not EntityType.PERSON:
            errors.append(_fe(["body", "relationships", i, "target"], "must be a PERSON entity"))
    return errors


def _fe(loc: list[str | int], msg: str) -> FieldError:
    return FieldError(loc=loc, msg=msg, type="value_error")


@router.get("/livez", include_in_schema=False)
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> Response:
    if request.app.state.ready and request.app.state.lane.is_open:
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "not_ready"}, status_code=503)


@router.post(
    "/v1/investigations",
    response_model=ContextInvestigationCreated,
    responses={200: {"description": "Identical create replayed (created=false)"}},
    status_code=201,
)
async def create_investigation(
    body: ContextInvestigationCreate,
    response: Response,
    audit: Annotated[AuditContext, Depends(authorize("investigations.create"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> ContextInvestigationCreated:
    if errors := _semantic_errors(body):
        raise ProblemError(
            422, "request body failed validation", code=ErrorCode.SCHEMA_INVALID, errors=errors
        )
    try:
        result = await repo.create_investigation(body, audit)
    except InvestigationClosed as exc:
        raise ProblemError(
            409, "investigation is closed", code=ErrorCode.INVESTIGATION_CANCELLED
        ) from exc
    except InvestigationContentConflict as exc:
        raise ProblemError(
            409,
            "investigation exists with different content",
            code=ErrorCode.CONTENT_CONFLICT,
        ) from exc
    if not result.created:
        response.status_code = 200
    return result


@router.post("/v1/investigations/{id}/close", response_model=ContextInvestigationClosed)
async def close_investigation(
    id: Annotated[str, Path(pattern=INVESTIGATION_ID_PATTERN)],
    body: ContextInvestigationClose,
    audit: Annotated[AuditContext, Depends(authorize("investigations.close"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> ContextInvestigationClosed:
    try:
        return await repo.close_investigation(id, body, audit)
    except InvestigationNotFound as exc:
        raise ProblemError(404, "investigation not found") from exc
