"""Frontend API, S1 subset (spec §12.1), plus probes.

Concurrency cap: ``429 Too Many Requests`` (it is a capacity limit, retryable once an
investigation ends, not a conflict with the resource being created). ``ProblemDetails.code`` is
omitted on 404/429 because ``ErrorCode`` has no value for them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from nexus_contracts.api.investigations import (
    Investigation,
    InvestigationCreateRequest,
    InvestigationPage,
)
from nexus_contracts.api.problem import FieldError, ProblemDetails
from nexus_contracts.common import InvestigationId
from nexus_contracts.events.timeline import TimelineEvent, TimelinePage
from nexus_orchestrator.api.problems import ProblemError, problem_response, schema_invalid
from nexus_orchestrator.api.runtime import Runtime, get_runtime
from nexus_orchestrator.pubsub import CLOSE
from nexus_orchestrator.scenarios import ScenarioList
from nexus_orchestrator.state.repository import InvalidCursor
from nexus_orchestrator.workflow.creation import ConcurrencyLimitReached, ScenarioNotFound

REPLAY_PAGE = 500
PROBLEM = {"model": ProblemDetails}

api = APIRouter(prefix="/api/v1")
probes = APIRouter()

RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
Limit = Annotated[int, Query(ge=1, le=100)]


def _bad_cursor(field: str) -> ProblemError:
    return ProblemError(
        422,
        "malformed cursor",
        code="schema_invalid",
        title="Request validation failed",
        errors=[FieldError(loc=["query", field], msg="malformed cursor", type="value_error")],
    )


async def _require_investigation(rt: Runtime, investigation_id: str) -> None:
    if not await rt.repo.investigation_exists(investigation_id):
        raise ProblemError(404, f"investigation {investigation_id} not found")


# -- scenarios ----------------------------------------------------------------------------------


@api.get("/scenarios", response_model=ScenarioList)
async def list_scenarios(rt: RuntimeDep) -> ScenarioList:
    return await rt.catalog.summaries()


# -- investigations ------------------------------------------------------------------------------


@api.post(
    "/investigations",
    status_code=201,
    response_model=Investigation,
    responses={404: PROBLEM, 422: PROBLEM, 429: PROBLEM},
)
async def create_investigation(body: InvestigationCreateRequest, rt: RuntimeDep) -> Investigation:
    try:
        return await rt.workflow.create(body)
    except ScenarioNotFound as exc:
        raise ProblemError(
            404, f"scenario {exc.args[0]} not found", title="Unknown scenario"
        ) from exc
    except ConcurrencyLimitReached as exc:
        raise ProblemError(
            429,
            str(exc),
            title="Too many concurrent investigations",
            headers={"Retry-After": "30"},
        ) from exc


@api.get("/investigations", response_model=InvestigationPage, responses={422: PROBLEM})
async def list_investigations(
    rt: RuntimeDep, limit: Limit = 20, cursor: str | None = None
) -> InvestigationPage:
    try:
        items, next_cursor = await rt.repo.list_investigations(limit, cursor)
    except InvalidCursor as exc:
        raise _bad_cursor("cursor") from exc
    return InvestigationPage(items=items, next_cursor=next_cursor)


@api.get(
    "/investigations/{investigation_id}", response_model=Investigation, responses={404: PROBLEM}
)
async def get_investigation(investigation_id: InvestigationId, rt: RuntimeDep) -> Investigation:
    investigation = await rt.repo.get_investigation(investigation_id)
    if investigation is None:
        raise ProblemError(404, f"investigation {investigation_id} not found")
    return investigation


@api.get(
    "/investigations/{investigation_id}/timeline",
    response_model=TimelinePage,
    responses={404: PROBLEM, 422: PROBLEM},
)
async def get_timeline(
    investigation_id: InvestigationId,
    rt: RuntimeDep,
    limit: Limit = 100,
    cursor: str | None = None,
) -> TimelinePage:
    await _require_investigation(rt, investigation_id)
    try:
        items, next_cursor = await rt.repo.timeline_page(investigation_id, limit, cursor)
    except InvalidCursor as exc:
        raise _bad_cursor("cursor") from exc
    return TimelinePage(items=items, next_cursor=next_cursor)


def _sse(event: TimelineEvent) -> ServerSentEvent:
    return ServerSentEvent(data=event.model_dump_json(), event=event.type.value, id=str(event.seq))


@api.get(
    "/investigations/{investigation_id}/events",
    response_class=EventSourceResponse,
    responses={404: PROBLEM, 422: PROBLEM},
)
async def stream_events(
    request: Request,
    investigation_id: InvestigationId,
    rt: RuntimeDep,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    """Replay ``seq > Last-Event-ID`` from SQLite, then live; no gap, no duplicate (D3)."""
    start = 0
    if last_event_id not in (None, ""):
        if not last_event_id.isdigit():
            return schema_invalid(
                request,
                [
                    FieldError(
                        loc=["header", "Last-Event-ID"],
                        msg="must be a non-negative integer",
                        type="int_parsing",
                    )
                ],
                "Last-Event-ID must be a timeline sequence number",
            )
        start = int(last_event_id)
    if not await rt.repo.investigation_exists(investigation_id):
        return problem_response(request, 404, detail=f"investigation {investigation_id} not found")

    async def generate() -> AsyncIterator[ServerSentEvent]:
        # Subscribe FIRST so anything committed during the replay is queued, not missed.
        sub = rt.broker.subscribe(investigation_id)
        try:
            last = start
            while True:
                page = await rt.repo.events_after(investigation_id, last, REPLAY_PAGE)
                for event in page:
                    yield _sse(event)
                    last = event.seq
                if len(page) < REPLAY_PAGE:
                    break
            while True:
                item = await sub.queue.get()
                if item is CLOSE:
                    return
                assert isinstance(item, TimelineEvent)
                if item.seq <= last:  # already sent during replay
                    continue
                yield _sse(item)
                last = item.seq
        finally:
            rt.broker.unsubscribe(sub)

    return EventSourceResponse(generate(), ping=rt.settings.sse_heartbeat_seconds)


# -- health ---------------------------------------------------------------------------------------


class HealthStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class ComponentHealth(BaseModel):
    name: str
    status: HealthStatus
    detail: str | None = None


class SystemHealth(BaseModel):
    status: HealthStatus
    components: list[ComponentHealth]


@api.get("/system/health", response_model=SystemHealth)
async def system_health(rt: RuntimeDep) -> SystemHealth:
    """Orchestrator not ready → ``DOWN``; any dependency down → ``DEGRADED``; else ``OK``."""
    orch = ComponentHealth(
        name="orchestrator",
        status=HealthStatus.OK if rt.ready else HealthStatus.DOWN,
        detail=None if rt.ready else _not_ready_reason(rt),
    )
    ok, detail = await rt.context.readyz()
    ctx = ComponentHealth(
        name="context-service",
        status=HealthStatus.OK if ok else HealthStatus.DOWN,
        detail=None if ok else detail,
    )
    if orch.status is HealthStatus.DOWN:
        overall = HealthStatus.DOWN
    elif ctx.status is not HealthStatus.OK:
        overall = HealthStatus.DEGRADED
    else:
        overall = HealthStatus.OK
    return SystemHealth(status=overall, components=[orch, ctx])


def _not_ready_reason(rt: Runtime) -> str:
    if rt.shutting_down:
        return "shutting down"
    if not rt.db.is_open:
        return "database not open"
    if not rt.leader.held:
        return "leader lock not held"
    return "startup sweep not finished"


# -- probes ---------------------------------------------------------------------------------------


@probes.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@probes.get("/readyz", responses={503: PROBLEM})
async def readyz(request: Request, rt: RuntimeDep) -> JSONResponse:
    if rt.ready:
        return JSONResponse({"status": "ready"})
    return problem_response(request, 503, detail=_not_ready_reason(rt))
