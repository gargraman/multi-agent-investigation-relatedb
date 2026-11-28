"""Frontend API, S1 + S2 subset (spec §12.1), plus probes.

S4a adds the graph-governance proxies (FR-220): graph, provenance, retract, contradictions, links
and assessment, all through the Context Service with the orchestrator token.

S2 adds ``GET /api/v1/investigations/{id}/findings`` (FR-009: proxies the Context Service
``InvestigationContext``; that service sits on an internal network), ``422
replay_objective_locked`` on create (FR-008) and one SystemHealth component per enabled agent with
its ``llm_mode`` (FR-010).

Concurrency cap: ``429 Too Many Requests`` (it is a capacity limit, retryable once an
investigation ends, not a conflict with the resource being created). ``ProblemDetails.code`` is
omitted on 404/429 because ``ErrorCode`` has no value for them.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from nexus_contracts.api.assessments import Assessment
from nexus_contracts.api.context import InvestigationContext
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.graph import ContradictionPair, Graph, HiddenLink, Provenance
from nexus_contracts.api.investigations import (
    Investigation,
    InvestigationCancelResponse,
    InvestigationCreateRequest,
    InvestigationPage,
)
from nexus_contracts.api.problem import FieldError, ProblemDetails
from nexus_contracts.api.retraction import RetractEvidenceRequest, RetractionImpact
from nexus_contracts.api.states import InvestigationStatus
from nexus_contracts.common import AssessmentId, InvestigationId, LlmMode, ManifestId
from nexus_contracts.events.timeline import EvidenceRetractedPayload, TimelineEvent, TimelinePage
from nexus_orchestrator.api.problems import ProblemError, problem_response, schema_invalid
from nexus_orchestrator.api.runtime import Runtime, get_runtime
from nexus_orchestrator.delegation.context_client import ProxyRead
from nexus_orchestrator.pubsub import CLOSE
from nexus_orchestrator.scenarios import ScenarioList
from nexus_orchestrator.state.repository import InvalidCursor
from nexus_orchestrator.workflow.creation import (
    ConcurrencyLimitReached,
    ReplayObjectiveLocked,
    ScenarioNotFound,
)

REPLAY_PAGE = 500
PROBLEM = {"model": ProblemDetails}
OPERATOR_ACTOR = "operator:demo"  # P0 has one user, `demo` (S4a FR-220)

log = logging.getLogger(__name__)

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
    except ReplayObjectiveLocked as exc:
        raise ProblemError(
            422,
            str(exc),
            code=ErrorCode.REPLAY_OBJECTIVE_LOCKED.value,
            title="Objective locked in replay mode",
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


def _terminal(status: InvestigationStatus) -> ProblemError:
    return ProblemError(
        409,
        f"investigation is {status.value}",
        code=ErrorCode.INVESTIGATION_TERMINAL.value,
        title="Investigation already ended",
    )


@api.post(
    "/investigations/{investigation_id}/cancel",
    response_model=InvestigationCancelResponse,
    responses={404: PROBLEM, 409: PROBLEM},
)
async def cancel_investigation(
    investigation_id: InvestigationId, rt: RuntimeDep
) -> InvestigationCancelResponse:
    """FR-125: fence-first cancel from CREATED, RUNNING or ANALYZING, all within the request
    (``Fence.end``: investigation row, Context Service close, agent cancels, tasks). The status
    is checked first so a refused cancel writes nothing, not even a ``transition.dropped`` note;
    losing a race to another ender shows up as ``applied=False`` and is the same 409. The driver
    is not stopped here: it leaves at its next tick (the investigation is no longer RUNNING), so
    a dispatch in flight finishes and cancels its own orphan (``Dispatcher._acked``)."""
    record = await rt.repo.investigation_record(investigation_id)
    if record is None:
        raise ProblemError(404, f"investigation {investigation_id} not found")
    if record.status.is_terminal:
        raise _terminal(record.status)
    with rt.tracer.start_as_current_span(
        "workflow.cancel", attributes={"nexus.investigation.id": investigation_id}
    ):
        result = await rt.fence.end(
            investigation_id, InvestigationStatus.CANCELLED, None, task_reason=None
        )
    if not result.applied:
        raise _terminal(result.status)
    return InvestigationCancelResponse(
        id=investigation_id, status=result.status, context_closed=result.context_closed
    )


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


@api.get(
    "/investigations/{investigation_id}/findings",
    response_model=InvestigationContext,
    responses={404: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
async def get_findings(investigation_id: InvestigationId, rt: RuntimeDep) -> InvestigationContext:
    """Committed findings, read through the Context Service (S2 FR-009, AC17 E2E)."""
    await _require_investigation(rt, investigation_id)
    read = await rt.context.get_context(investigation_id)
    if read.context is not None:
        return read.context
    if read.status == 404:
        raise ProblemError(
            404, f"investigation {investigation_id} is not registered with the Context Service yet"
        )
    raise ProblemError(
        read.status,
        f"Context Service: {read.detail}",
        code=ErrorCode.CONTEXT_UNAVAILABLE.value,
        title="Context Service unavailable",
    )


# -- S4a graph-governance proxies (FR-220) ---------------------------------------------------


def _unwrap[T](read: ProxyRead[T], what: str) -> T:
    """The value, or the Context Service's failure as our ProblemDetails: its 404 stays 404,
    unreachable / 5xx / unreadable body is 502 ``context_unavailable``, any other 4xx passes
    through with its detail and code."""
    if read.value is not None:
        return read.value
    if read.unavailable:
        raise _context_unavailable(read.detail)
    if read.status == 404:
        raise ProblemError(404, read.detail if read.detail != "HTTP 404" else f"{what} not found")
    raise ProblemError(read.status, read.detail, code=read.code)


def _context_unavailable(detail: str) -> ProblemError:
    return ProblemError(
        502,
        f"Context Service: {detail}",
        code=ErrorCode.CONTEXT_UNAVAILABLE.value,
        title="Context Service unavailable",
    )


@api.get(
    "/investigations/{investigation_id}/graph",
    response_model=Graph,
    responses={404: PROBLEM, 502: PROBLEM},
)
async def get_graph(investigation_id: InvestigationId, rt: RuntimeDep) -> Graph:
    """I6 knowledge-graph view, read through the Context Service."""
    await _require_investigation(rt, investigation_id)
    return _unwrap(await rt.context.get_graph(investigation_id), "investigation")


@api.get(
    "/assessments/{assessment_id}/provenance",
    response_model=Provenance,
    responses={404: PROBLEM, 502: PROBLEM},
)
async def get_provenance(assessment_id: AssessmentId, rt: RuntimeDep) -> Provenance:
    """I1 provenance. Keyed by assessment alone, so there is no local investigation check:
    the Context Service owns assessment ids."""
    return _unwrap(await rt.context.get_provenance(assessment_id), "assessment")


@api.get(
    "/investigations/{investigation_id}/entities/{entity_id}/contradictions",
    response_model=list[ContradictionPair],
    responses={404: PROBLEM, 502: PROBLEM},
)
async def get_contradictions(
    investigation_id: InvestigationId, entity_id: ManifestId, rt: RuntimeDep
) -> list[ContradictionPair]:
    """I3 contradiction neighbourhood of one entity."""
    await _require_investigation(rt, investigation_id)
    read = await rt.context.get_contradictions(investigation_id, entity_id)
    return _unwrap(read, "entity")


@api.get(
    "/investigations/{investigation_id}/links",
    response_model=list[HiddenLink],
    responses={404: PROBLEM, 502: PROBLEM},
)
async def get_links(investigation_id: InvestigationId, rt: RuntimeDep) -> list[HiddenLink]:
    """I4 cross-agent hidden links (``from`` stays the wire name of a window's start)."""
    await _require_investigation(rt, investigation_id)
    return _unwrap(await rt.context.get_links(investigation_id), "investigation")


@api.get(
    "/investigations/{investigation_id}/assessment",
    response_model=Assessment,
    responses={404: PROBLEM, 502: PROBLEM},
)
async def get_assessment(investigation_id: InvestigationId, rt: RuntimeDep) -> Assessment:
    """The investigation's assessment, taken from the Context Service ``/context`` read; 404
    until one has been submitted."""
    await _require_investigation(rt, investigation_id)
    read = await rt.context.get_context(investigation_id)
    if read.context is None:
        if read.status == 404:
            raise ProblemError(
                404,
                f"investigation {investigation_id} is not registered with the Context Service yet",
            )
        raise _context_unavailable(read.detail)
    if read.context.assessment is None:
        raise ProblemError(404, f"investigation {investigation_id} has no assessment yet")
    return read.context.assessment


@api.post(
    "/investigations/{investigation_id}/evidence/{evidence_id}/retract",
    response_model=RetractionImpact,
    status_code=201,
    responses={
        200: {"description": "Evidence was already retracted: the original impact set"},
        404: PROBLEM,
        502: PROBLEM,
    },
)
async def retract_evidence(
    investigation_id: InvestigationId,
    evidence_id: ManifestId,
    response: Response,
    rt: RuntimeDep,
    body: RetractEvidenceRequest | None = None,
) -> RetractionImpact:
    """I2 retraction, sent to the Context Service as ``operator:demo``. Answers with the Context
    Service's own status: 201 when newly retracted (and then one ``evidence.retracted`` event,
    counts only, is appended to this investigation's timeline), 200 when it already was (no
    event, so a retry or double click never duplicates one).

    The timeline is a projection of a retraction that is already committed in the Context
    Service, so a failed append is logged and the 201 still goes out; failing the request would
    invite a retry that returns 200 and never writes the missing event anyway."""
    await _require_investigation(rt, investigation_id)
    read = await rt.context.retract_evidence(investigation_id, evidence_id, OPERATOR_ACTOR, body)
    impact = _unwrap(read, "evidence")
    response.status_code = read.status
    if read.status == 201:
        payload = EvidenceRetractedPayload(
            evidence_id=impact.evidence_id,
            decision_id=impact.decision_id,
            findings=len(impact.finding_ids),
            assertions=len(impact.assertion_ids),
            assessments=len(impact.assessment_ids),
        ).model_dump(mode="json")
        try:
            await rt.repo.append_retraction_event(investigation_id, payload, rt.clock.now())
        except Exception:
            log.exception(
                "evidence.retracted not recorded on the timeline",
                extra={"investigation_id": investigation_id, "evidence_id": evidence_id},
            )
    return impact


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
    llm_mode: LlmMode | None = None  # agents only (ADR-006 E8, S2 FR-010)


class SystemHealth(BaseModel):
    status: HealthStatus
    components: list[ComponentHealth]


@api.get("/system/health", response_model=SystemHealth)
async def system_health(rt: RuntimeDep) -> SystemHealth:
    """Orchestrator not ready → ``DOWN``; any dependency (Context Service, an enabled agent) down
    → ``DEGRADED``; else ``OK``. Agents are probed via their card, which also refreshes the E4
    cache; ``llm_mode`` is the last mode the agent reported."""
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
    agents = []
    for agent in rt.registry.agents:
        state = await rt.registry.check(agent)
        agents.append(
            ComponentHealth(
                name=agent.value,
                status=HealthStatus.OK if state.reachable else HealthStatus.DOWN,
                detail=None if state.reachable else state.detail,
                llm_mode=state.llm_mode,
            )
        )
    deps = [ctx, *agents]
    if orch.status is HealthStatus.DOWN:
        overall = HealthStatus.DOWN
    elif any(c.status is not HealthStatus.OK for c in deps):
        overall = HealthStatus.DEGRADED
    else:
        overall = HealthStatus.OK
    return SystemHealth(status=overall, components=[orch, *deps])


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
