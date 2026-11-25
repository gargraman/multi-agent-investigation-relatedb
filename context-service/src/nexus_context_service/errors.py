"""RFC 7807 errors (spec §12): every non-2xx response is ``application/problem+json``."""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nexus_context_service.lane import LaneUnavailable
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.problem import PROBLEM_JSON_MEDIA_TYPE, FieldError, ProblemDetails

log = logging.getLogger(__name__)


class ProblemError(Exception):
    """Raise anywhere in a request to return a ProblemDetails response."""

    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        code: ErrorCode | None = None,
        errors: list[FieldError] | None = None,
        headers: dict[str, str] | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or HTTPStatus(status).phrase)
        self.status = status
        self.detail = detail
        self.code = code
        self.errors = errors
        self.headers = headers
        self.extensions = extensions  # extra RFC 7807 members, e.g. finding_ids on 409


def problem_response(
    request: Request,
    status: int,
    detail: str | None = None,
    *,
    code: str | None = None,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    body = ProblemDetails(
        title=HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        instance=request.url.path,
        code=code,
        errors=errors,
        **(extensions or {}),
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        headers=headers,
    )


def _field_errors(exc: RequestValidationError) -> list[FieldError]:
    # Never echo the offending input back (it may be content): loc, msg and type only.
    return [
        FieldError(
            loc=[p for p in e.get("loc", ()) if isinstance(p, str | int)],
            msg=str(e.get("msg", "")),
            type=str(e.get("type", "")),
        )
        for e in exc.errors()
    ]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(
            request,
            exc.status,
            exc.detail,
            code=exc.code.value if exc.code else None,
            errors=exc.errors,
            headers=exc.headers,
            extensions=exc.extensions,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            request,
            422,
            "request body failed schema validation",
            code=ErrorCode.SCHEMA_INVALID.value,
            errors=_field_errors(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem_response(request, exc.status_code, detail, headers=exc.headers)

    @app.exception_handler(LaneUnavailable)
    async def _lane(request: Request, exc: LaneUnavailable) -> JSONResponse:
        return problem_response(request, 503, "graph store is not ready")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Type only: Relate error text can carry values (spec §15.6).
        log.error("unhandled error", extra={"error_type": type(exc).__name__})
        return problem_response(request, 500, "internal error")
