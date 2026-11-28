"""HTTP routes (spec §12.3). S1: investigation create/close + health probes. S2: findings batch
and the investigation context read model."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse

from nexus_context_service.auth import authorize
from nexus_context_service.errors import ProblemError
from nexus_context_service.repository import (
    AlreadyCommitted,
    AuditContext,
    BatchInvalid,
    GraphRepository,
    InvestigationClosed,
    InvestigationContentConflict,
    InvestigationNotFound,
    TaskOwnedByOtherCaller,
)
from nexus_context_service.scope import ReadScope, read_scope
from nexus_contracts.api.context import (
    ContextInvestigationClose,
    ContextInvestigationClosed,
    ContextInvestigationCreate,
    ContextInvestigationCreated,
    InvestigationContext,
    SearchRequest,
    SearchResponse,
)
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.findings import (
    AlreadyCommittedProblem,
    FindingBatchRequest,
    FindingBatchResponse,
)
from nexus_contracts.api.problem import PROBLEM_JSON_MEDIA_TYPE, FieldError, ProblemDetails
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


def _problem_doc(model: type, description: str) -> dict:
    """OpenAPI entry for an error response (served as application/problem+json)."""
    return {"model": model, "description": f"{description} ({PROBLEM_JSON_MEDIA_TYPE})"}


@router.post(
    "/v1/findings:batch",
    response_model=FindingBatchResponse,
    status_code=201,
    responses={
        200: {"description": "Identical batch replayed for this task_id: original finding ids"},
        404: _problem_doc(ProblemDetails, "Unknown investigation"),
        403: _problem_doc(ProblemDetails, "caller not allowed, or task_id owned by another caller"),
        409: _problem_doc(
            AlreadyCommittedProblem,
            "already_committed (different content; carries the committed finding_ids) or "
            "investigation_cancelled (investigation is CLOSED; ProblemDetails)",
        ),
        422: _problem_doc(ProblemDetails, "schema_invalid or evidence_integrity_error"),
    },
)
async def submit_findings(
    body: FindingBatchRequest,
    request: Request,
    response: Response,
    audit: Annotated[AuditContext, Depends(authorize("findings.submit"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> FindingBatchResponse:
    """S2 FR-040–FR-043: one lane transaction, idempotent on ``findings:{task_id}``. The replay
    check runs before the CLOSED fence (G15)."""
    try:
        result = await repo.submit_finding_batch(
            body, audit, request.app.state.policy.write_clearance(audit.caller)
        )
    except InvestigationNotFound as exc:
        raise ProblemError(404, "investigation not found") from exc
    except TaskOwnedByOtherCaller as exc:
        # Generic on purpose: no task_id, owner or ids (S3 FR-130); the denial is audited.
        raise ProblemError(403, "forbidden", code=ErrorCode.FORBIDDEN) from exc
    except AlreadyCommitted as exc:
        raise ProblemError(
            409,
            "a batch with different content was already committed for this task",
            code=ErrorCode.ALREADY_COMMITTED,
            extensions={"finding_ids": exc.finding_ids},
        ) from exc
    except InvestigationClosed as exc:
        raise ProblemError(
            409, "investigation is closed", code=ErrorCode.INVESTIGATION_CANCELLED
        ) from exc
    except BatchInvalid as exc:
        code = (
            ErrorCode.EVIDENCE_INTEGRITY_ERROR if exc.integrity_error else ErrorCode.SCHEMA_INVALID
        )
        raise ProblemError(
            422, "findings batch failed validation", code=code, errors=exc.errors
        ) from exc
    if not result.created:
        response.status_code = 200
    return FindingBatchResponse(task_id=body.task_id, finding_ids=result.finding_ids)


@router.get(
    "/v1/investigations/{id}/context",
    response_model=InvestigationContext,
    responses={
        400: _problem_doc(ProblemDetails, "analysis-agent read without a valid X-Nexus-Task-Id"),
        404: _problem_doc(ProblemDetails, "Unknown investigation"),
    },
)
async def investigation_context(
    id: Annotated[str, Path(pattern=INVESTIGATION_ID_PATTERN)],
    scope: Annotated[ReadScope, Depends(read_scope("investigations.context"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> InvestigationContext:
    try:
        return await repo.get_context(id, scope)
    except InvestigationNotFound as exc:
        raise ProblemError(404, "investigation not found") from exc


_SEARCH_RESPONSES = {
    400: _problem_doc(
        ProblemDetails,
        "analysis-agent read without a valid X-Nexus-Task-Id, or body investigation_id differs "
        "from the path investigation",
    ),
    404: _problem_doc(ProblemDetails, "Unknown investigation"),
}


async def _search(body: SearchRequest, scope: ReadScope, repo: GraphRepository) -> SearchResponse:
    try:
        return await repo.search(body, scope)
    except InvestigationNotFound as exc:
        raise ProblemError(404, "investigation not found") from exc


@router.post("/v1/search", response_model=SearchResponse, responses=_SEARCH_RESPONSES)
async def search(
    body: SearchRequest,
    scope: Annotated[ReadScope, Depends(read_scope("search"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> SearchResponse:
    """S4a FR-208: structured, clearance-filtered search of one investigation's findings."""
    return await _search(body, scope, repo)


@router.post(
    "/v1/investigations/{id}/search", response_model=SearchResponse, responses=_SEARCH_RESPONSES
)
async def search_in_investigation(
    id: Annotated[str, Path(pattern=INVESTIGATION_ID_PATTERN)],
    body: SearchRequest,
    scope: Annotated[ReadScope, Depends(read_scope("search"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> SearchResponse:
    """The same search addressed by path. The body's ``investigation_id`` must be this path's
    investigation: a mismatch is rejected rather than silently searching the body's one."""
    if body.investigation_id != id:
        raise ProblemError(
            400,
            "body investigation_id does not match the path investigation",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return await _search(body, scope, repo)
