"""RFC 7807 everywhere (spec §12): every error leaves as ``application/problem+json``.

- Request validation → ``422`` with ``code=schema_invalid`` and ``errors[]``.
- ``ProblemError`` → its own status/code.
- Starlette HTTP errors (404 route, 405 …) → plain problem with that status.
- Anything else → ``500`` without internals (logged with the trace id).
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.problem import PROBLEM_JSON_MEDIA_TYPE, FieldError, ProblemDetails

log = logging.getLogger(__name__)


class ProblemError(Exception):
    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        code: str | None = None,
        title: str | None = None,
        errors: list[FieldError] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or title or str(status))
        self.status = status
        self.detail = detail
        self.code = code
        self.title = title or HTTPStatus(status).phrase
        self.errors = errors
        self.headers = headers


def problem_response(
    request: Request,
    status: int,
    *,
    title: str | None = None,
    detail: str | None = None,
    code: str | None = None,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ProblemDetails(
        title=title or HTTPStatus(status).phrase,
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


def schema_invalid(request: Request, errors: list[FieldError], detail: str) -> JSONResponse:
    return problem_response(
        request,
        422,
        title="Request validation failed",
        detail=detail,
        code=ErrorCode.SCHEMA_INVALID.value,
        errors=errors,
    )


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(
            request,
            exc.status,
            title=exc.title,
            detail=exc.detail,
            code=exc.code,
            errors=exc.errors,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            FieldError(
                loc=[p if isinstance(p, int | str) else str(p) for p in e.get("loc", ())],
                msg=str(e.get("msg", "")),
                type=str(e.get("type", "value_error")),
            )
            for e in exc.errors()
        ]
        return schema_invalid(request, errors, f"{len(errors)} validation error(s)")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem_response(
            request, exc.status_code, detail=detail, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        return problem_response(request, 500, detail="internal error")
