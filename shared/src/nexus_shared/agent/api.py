"""Agent task API (spec §12.2, ADR-004; S2 FR-020–FR-023).

- ``POST /v1/tasks`` (orchestrator token only): 202 new (SUBMITTED or REJECTED), 200 duplicate
  with the current status, 409 ``content_conflict`` / ``stale_attempt``, 422 malformed body.
  A new, higher attempt supersedes running lower ones: they end CANCELED (S3).
- ``GET /v1/tasks/{task_id}``: TaskResult of the highest attempt; 404 if unknown.
- ``POST /v1/tasks/{task_id}/cancel`` (orchestrator token only): 200 ``TaskCancelResponse`` with
  the latest attempt's status (idempotent; a terminal row is left as is); 404 if unknown.
- ``GET /.well-known/agent-card.json``, ``/livez``, ``/readyz``: unauthenticated (G20).

Every error is ``application/problem+json`` (spec §12). Agent-specific behaviour (identity, task
types, submit-time policy) comes from the :class:`AgentSpec` on ``app.state.spec``.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nexus_contracts.a2a.task import (
    AgentCard,
    TaskCancelResponse,
    TaskRequest,
    TaskResult,
    TaskSubmitResponse,
)
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.problem import PROBLEM_JSON_MEDIA_TYPE, FieldError, ProblemDetails
from nexus_contracts.common import Classification
from nexus_shared.agent.catalog import ManifestCatalog
from nexus_shared.agent.runner import TaskRunner
from nexus_shared.agent.settings import AgentSettings
from nexus_shared.agent.spec import AgentSpec
from nexus_shared.agent.store import SubmitDecision, TaskStore
from nexus_shared.security import ORCHESTRATOR, TokenRegistry

log = logging.getLogger(__name__)

router = APIRouter()


class ProblemError(Exception):
    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        code: ErrorCode | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or HTTPStatus(status).phrase)
        self.status = status
        self.detail = detail
        self.code = code
        self.headers = headers


def problem_response(
    request: Request,
    status: int,
    detail: str | None = None,
    *,
    code: str | None = None,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ProblemDetails(
        title=HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        instance=request.url.path,
        code=code,
        errors=errors,
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(
            request,
            exc.status,
            exc.detail,
            code=exc.code.value if exc.code else None,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            FieldError(
                loc=[p for p in e.get("loc", ()) if isinstance(p, str | int)],
                msg=str(e.get("msg", "")),
                type=str(e.get("type", "")),
            )
            for e in exc.errors()
        ]
        return problem_response(
            request,
            422,
            "request body failed schema validation",
            code=ErrorCode.SCHEMA_INVALID.value,
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem_response(request, exc.status_code, detail, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", extra={"error_type": type(exc).__name__})
        return problem_response(request, 500, "internal error")


async def require_orchestrator(request: Request) -> str:
    registry: TokenRegistry = request.app.state.token_registry
    caller = registry.authenticate(request.headers.get("Authorization"))
    if caller != ORCHESTRATOR:
        raise ProblemError(
            401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
        )
    return caller


Caller = Annotated[str, Depends(require_orchestrator)]


def agent_card(spec: AgentSpec, settings: AgentSettings) -> AgentCard:
    return AgentCard(
        id=spec.agent_id,
        role=spec.role,
        task_types=list(spec.task_types),
        version=spec.version,
        clearance=Classification.DEMO_HIGH,
        llm_mode=settings.nexus_llm_mode,
    )


@router.post(
    "/v1/tasks",
    response_model=TaskSubmitResponse,
    status_code=202,
    responses={200: {"description": "Duplicate submit: current status"}},
)
async def submit_task(body: TaskRequest, request: Request, _caller: Caller) -> JSONResponse:
    state = request.app.state
    spec: AgentSpec = state.spec
    settings: AgentSettings = state.settings
    store: TaskStore = state.store
    catalog: ManifestCatalog = state.catalog
    runner: TaskRunner = state.runner

    rejected = spec.validate_request(body, catalog.get(body.scenario_id))
    decision, row = await store.submit(body, llm_mode=settings.nexus_llm_mode, rejected=rejected)
    if decision is SubmitDecision.STALE:
        raise ProblemError(
            409, "attempt is lower than the latest stored attempt", code=ErrorCode.STALE_ATTEMPT
        )
    if decision is SubmitDecision.CONFLICT:
        raise ProblemError(
            409,
            "task_id/attempt already submitted with different content",
            code=ErrorCode.CONTENT_CONFLICT,
        )
    response = TaskSubmitResponse(task_id=row["task_id"], status=row.status)
    if decision is SubmitDecision.DUPLICATE:
        return JSONResponse(response.model_dump(mode="json"), status_code=200)
    log.info(
        "task accepted",
        extra={
            "task_id": body.task_id,
            "attempt": body.attempt,
            "investigation_id": body.investigation_id,
            "status": row.status.value,
        },
    )
    if body.attempt > 1:
        superseded = await store.supersede_lower_attempts(body.task_id, body.attempt)
        if superseded:
            log.info(
                "lower attempts superseded",
                extra={"task_id": body.task_id, "attempt": body.attempt, "superseded": superseded},
            )
        runner.cancel(body.task_id)  # only lower attempts can be running at this point
    if rejected is None:
        runner.start(body)
    return JSONResponse(response.model_dump(mode="json"), status_code=202)


@router.get("/v1/tasks/{task_id}", response_model=TaskResult)
async def get_task(task_id: str, request: Request, _caller: Caller) -> TaskResult:
    row = await request.app.state.store.latest(task_id)
    if row is None:
        raise ProblemError(404, "unknown task")
    return row.to_result()


@router.post("/v1/tasks/{task_id}/cancel", response_model=TaskCancelResponse)
async def cancel_task(task_id: str, request: Request, _caller: Caller) -> TaskCancelResponse:
    state = request.app.state
    row = await state.store.request_cancel(task_id)
    if row is None:
        raise ProblemError(404, "unknown task")
    state.runner.cancel(task_id)
    log.info(
        "task cancel requested",
        extra={"task_id": task_id, "attempt": row["attempt"], "status": row.status.value},
    )
    return TaskCancelResponse(task_id=row["task_id"], status=row.status)


@router.get("/.well-known/agent-card.json", response_model=AgentCard)
async def get_agent_card(request: Request) -> AgentCard:
    return agent_card(request.app.state.spec, request.app.state.settings)


@router.get("/livez", include_in_schema=False)
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    mode = state.settings.nexus_llm_mode.value
    checks = {
        "sqlite": state.store.is_open,
        "manifests": state.catalog.is_ready(),
    }
    ok = state.ready and all(checks.values())
    body = {"status": "ready" if ok else "not_ready", "llm_mode": mode, "checks": checks}
    return JSONResponse(body, status_code=200 if ok else 503)
