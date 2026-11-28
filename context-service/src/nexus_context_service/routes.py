"""HTTP routes (spec §12.3). S1: investigation create/close + health probes. S2: findings batch
and the investigation context read model. S4a: search and assessment submission."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse

from nexus_context_service.assessments import CitationRejected
from nexus_context_service.auth import authorize
from nexus_context_service.errors import ProblemError
from nexus_context_service.lane import LaneUnavailable
from nexus_context_service.repository import (
    AlreadyCommitted,
    AssessmentAlreadyCommitted,
    AssessmentNotFound,
    AuditContext,
    BatchInvalid,
    EntityNotFound,
    EvidenceNotFound,
    GraphRepository,
    InvestigationClosed,
    InvestigationContentConflict,
    InvestigationNotFound,
    TaskOwnedByOtherCaller,
)
from nexus_context_service.scope import ReadScope, read_scope
from nexus_contracts.api.assessments import AssessmentSubmission, AssessmentSubmissionResponse
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
from nexus_contracts.api.graph import ContradictionPair, Graph, HiddenLink, Provenance
from nexus_contracts.api.problem import PROBLEM_JSON_MEDIA_TYPE, FieldError, ProblemDetails
from nexus_contracts.api.retraction import RetractEvidenceRequest, RetractionImpact
from nexus_contracts.common import MANIFEST_ID_PATTERN, EntityType

router = APIRouter()
log = logging.getLogger(__name__)

INVESTIGATION_ID_PATTERN = r"^inv_[A-Za-z0-9_-]{1,64}$"
ASSESSMENT_ID_PATTERN = r"^asm_[A-Za-z0-9_-]{1,64}$"


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


@router.post(
    "/v1/search",
    response_model=SearchResponse,
    responses={
        400: _problem_doc(ProblemDetails, "analysis-agent read without a valid X-Nexus-Task-Id"),
        404: _problem_doc(ProblemDetails, "Unknown investigation"),
    },
)
async def search(
    body: SearchRequest,
    scope: Annotated[ReadScope, Depends(read_scope("search"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> SearchResponse:
    """S4a FR-208: structured, clearance-filtered search of one investigation's findings."""
    try:
        return await repo.search(body, scope)
    except InvestigationNotFound as exc:
        raise ProblemError(404, "investigation not found") from exc


@router.post(
    "/v1/assessments",
    response_model=AssessmentSubmissionResponse,
    status_code=201,
    responses={
        200: {"description": "Identical assessment replayed for this task_id: original response"},
        403: _problem_doc(ProblemDetails, "caller not allowed"),
        404: _problem_doc(ProblemDetails, "Unknown investigation"),
        409: _problem_doc(
            ProblemDetails,
            "already_committed (different content; carries the committed assessment_id) or "
            "investigation_cancelled (investigation is CLOSED)",
        ),
        422: _problem_doc(
            ProblemDetails,
            "schema_invalid, or unknown_finding_reference (a cited finding was not retrieved by "
            "this task in this investigation, or is not visible; never says which)",
        ),
    },
)
async def submit_assessment(
    body: AssessmentSubmission,
    request: Request,
    response: Response,
    audit: Annotated[AuditContext, Depends(authorize("assessments.create"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> AssessmentSubmissionResponse:
    """S4a FR-209..212: one lane transaction, idempotent on ``assessment:{task_id}``; citations
    are checked against the task's retrieval receipt after the replay check."""
    try:
        result, created = await repo.submit_assessment(
            body, audit, max_rank=request.app.state.policy.read_rank(audit.caller)
        )
    except InvestigationNotFound as exc:
        raise ProblemError(404, "investigation not found") from exc
    except InvestigationClosed as exc:
        raise ProblemError(
            409, "investigation is closed", code=ErrorCode.INVESTIGATION_CANCELLED
        ) from exc
    except AssessmentAlreadyCommitted as exc:
        raise ProblemError(
            409,
            "an assessment with different content was already committed for this task",
            code=ErrorCode.ALREADY_COMMITTED,
            extensions={"assessment_id": exc.assessment_id},
        ) from exc
    except CitationRejected as exc:
        # Generic on purpose: no id, no reason (missing / foreign / hidden / not retrieved).
        raise ProblemError(
            422,
            "assessment cites a finding outside this task's retrieval",
            code=ErrorCode.UNKNOWN_FINDING_REFERENCE,
        ) from exc
    if not created:
        response.status_code = 200
    return result


@router.post(
    "/v1/investigations/{id}/evidence/{evidence_id}/retract",
    response_model=RetractionImpact,
    status_code=201,
    responses={
        200: {"description": "Evidence was already retracted: the original impact set"},
        403: _problem_doc(ProblemDetails, "caller not allowed"),
        404: _problem_doc(ProblemDetails, "Unknown investigation or evidence"),
    },
)
async def retract_evidence(
    id: Annotated[str, Path(pattern=INVESTIGATION_ID_PATTERN)],
    evidence_id: Annotated[str, Path(pattern=MANIFEST_ID_PATTERN, max_length=128)],
    response: Response,
    audit: Annotated[AuditContext, Depends(authorize("evidence.retract"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
    body: RetractEvidenceRequest | None = None,
) -> RetractionImpact:
    """S4a FR-216 (I2): one lane transaction. 201 when newly retracted, 200 (nothing written)
    when it already was; the orchestrator proxy relies on that to append its timeline event
    once. Allowed on a closed investigation."""
    try:
        impact, created = await repo.retract_evidence(
            id, evidence_id, body.reason if body else None, audit
        )
    except (InvestigationNotFound, EvidenceNotFound) as exc:
        raise ProblemError(404, "evidence not found") from exc
    if not created:
        response.status_code = 200
    return impact


@router.get(
    "/v1/assessments/{assessment_id}/provenance",
    response_model=Provenance,
    responses={
        403: _problem_doc(ProblemDetails, "caller not allowed"),
        404: _problem_doc(
            ProblemDetails, "Unknown assessment, or one above the caller's clearance"
        ),
    },
)
async def assessment_provenance(
    assessment_id: Annotated[str, Path(pattern=ASSESSMENT_ID_PATTERN)],
    request: Request,
    audit: Annotated[AuditContext, Depends(authorize("assessments.provenance"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> Provenance:
    """S4a FR-215 (I1). Above clearance is the same 404 as unknown (§9.5), plus a ``DENIED``
    audit event in its own lane run. A successful read writes no audit event."""
    try:
        return await repo.provenance(
            assessment_id, request.app.state.policy.read_rank(audit.caller)
        )
    except AssessmentNotFound as exc:
        if exc.hidden:
            try:  # the read transaction rolled back with the raise: audit in its own run
                await repo.record_denial(audit, "assessments.provenance", exc.investigation_id)
            except LaneUnavailable:
                log.warning("denial not audited: store not ready", extra={"op": "provenance"})
        raise ProblemError(404, "assessment not found") from exc


@router.get(
    "/v1/investigations/{id}/entities/{entity_id}/contradictions",
    response_model=list[ContradictionPair],
    responses={
        403: _problem_doc(ProblemDetails, "caller not allowed"),
        404: _problem_doc(ProblemDetails, "Unknown investigation or entity"),
    },
)
async def entity_contradictions(
    id: Annotated[str, Path(pattern=INVESTIGATION_ID_PATTERN)],
    entity_id: Annotated[str, Path(pattern=MANIFEST_ID_PATTERN, max_length=128)],
    request: Request,
    audit: Annotated[AuditContext, Depends(authorize("entities.contradictions"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> list[ContradictionPair]:
    """S4a FR-217 (I3): each contradicting pair once, lower id first, both within clearance.
    A successful read writes no audit event."""
    try:
        return await repo.contradictions(
            id, entity_id, request.app.state.policy.read_rank(audit.caller)
        )
    except EntityNotFound as exc:
        raise ProblemError(404, "entity not found") from exc


@router.get(
    "/v1/investigations/{id}/links",
    response_model=list[HiddenLink],
    responses={
        403: _problem_doc(ProblemDetails, "caller not allowed"),
        404: _problem_doc(ProblemDetails, "Unknown investigation"),
    },
)
async def investigation_links(
    id: Annotated[str, Path(pattern=INVESTIGATION_ID_PATTERN)],
    request: Request,
    audit: Annotated[AuditContext, Depends(authorize("investigations.links"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> list[HiddenLink]:
    """S4a FR-218 (I4): cross-agent hidden links, within the caller's clearance. A successful
    read writes no audit event."""
    try:
        return await repo.links(id, request.app.state.policy.read_rank(audit.caller))
    except InvestigationNotFound as exc:
        raise ProblemError(404, "investigation not found") from exc


@router.get(
    "/v1/investigations/{id}/graph",
    response_model=Graph,
    responses={
        403: _problem_doc(ProblemDetails, "caller not allowed"),
        404: _problem_doc(ProblemDetails, "Unknown investigation"),
    },
)
async def investigation_graph(
    id: Annotated[str, Path(pattern=INVESTIGATION_ID_PATTERN)],
    scope: Annotated[ReadScope, Depends(read_scope("investigations.graph"))],
    repo: Annotated[GraphRepository, Depends(_repo)],
    _: Annotated[None, Depends(_require_ready)],
) -> Graph:
    """S4a FR-219 (I6): the knowledge-graph view within the caller's clearance. Writes one read
    audit event (FR-204): ``FILTERED`` with the hidden count when anything was dropped."""
    try:
        return await repo.graph(id, scope)
    except InvestigationNotFound as exc:
        raise ProblemError(404, "investigation not found") from exc
