"""S4a FR-220 / Proxy-a: orchestrator proxies for I1-I4, I6, retraction and assessment, plus the
``evidence.retracted`` timeline event. The Context Service is ``FakeContextService`` (canned
bodies at the HTTP boundary), so these pin what the proxy sends and how it maps errors, not the
service's own behaviour."""

from __future__ import annotations

import pytest
from orch_testkit import SCENARIO, TOKEN, FakeAgent, FakeContextService, running, runtime
from pydantic import ValidationError

from nexus_contracts.api.assessments import Assessment
from nexus_contracts.api.graph import ContradictionPair, Graph, HiddenLink, Provenance
from nexus_contracts.api.retraction import RetractionImpact
from nexus_contracts.events.timeline import (
    EvidenceRetractedPayload,
    TimelineEvent,
    TimelineEventType,
)
from nexus_orchestrator.ids import utcnow
from nexus_shared.security import ACTOR_HEADER

ASSESSMENT = {
    "assessment_id": "asm_1",
    "investigation_id": "inv_PLACEHOLDER",
    "status": "ACTIVE",
    "summary": "s",
    "conclusions": [
        {
            "statement": "c",
            "kind": "INFERENCE",
            "confidence": 0.7,
            "confidence_band": "MODERATE",
            "supporting_finding_ids": ["fnd_1"],
        }
    ],
    "model_id": "m",
    "prompt_version": "p1",
}


@pytest.fixture
def fake() -> FakeContextService:
    return FakeContextService()


@pytest.fixture
def app(make_app, fake):
    agent = FakeAgent()
    agent.hold = True
    return make_app(fake, agent)


async def _create(client) -> str:
    resp = await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _timeline(client, inv_id: str) -> list[TimelineEvent]:
    resp = await client.get(f"/api/v1/investigations/{inv_id}/timeline", params={"limit": 100})
    return [TimelineEvent.model_validate(e) for e in resp.json()["items"]]


def _retracted(events: list[TimelineEvent]) -> list[TimelineEvent]:
    return [e for e in events if e.type is TimelineEventType.EVIDENCE_RETRACTED]


# (method, path template, model validating the body)
ROUTES = [
    pytest.param("GET", "/investigations/{inv}/graph", Graph, 200, id="graph"),
    pytest.param("GET", "/assessments/asm_1/provenance", Provenance, 200, id="provenance"),
    pytest.param(
        "POST", "/investigations/{inv}/evidence/ev-1/retract", RetractionImpact, 201, id="retract"
    ),
    pytest.param(
        "GET",
        "/investigations/{inv}/entities/ent-1/contradictions",
        list[ContradictionPair],
        200,
        id="contradictions",
    ),
    pytest.param("GET", "/investigations/{inv}/links", list[HiddenLink], 200, id="links"),
    pytest.param("GET", "/investigations/{inv}/assessment", Assessment, 200, id="assessment"),
]


def _validate(model, data):
    from pydantic import TypeAdapter

    return TypeAdapter(model).validate_python(data)


async def test_each_proxy_passes_through(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        fake.assessment = {**ASSESSMENT, "investigation_id": inv}
        for method, template, model, status in [
            (p.values[0], p.values[1], p.values[2], p.values[3]) for p in ROUTES
        ]:
            resp = await client.request(method, "/api/v1" + template.format(inv=inv))
            assert resp.status_code == status, (template, resp.text)
            _validate(model, resp.json())
        assert fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"


async def test_graph_and_links_bodies_are_the_context_service_bodies(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        graph = await client.get(f"/api/v1/investigations/{inv}/graph")
        links = await client.get(f"/api/v1/investigations/{inv}/links")
        pairs = await client.get(f"/api/v1/investigations/{inv}/entities/ent-1/contradictions")
        prov = await client.get("/api/v1/assessments/asm_1/provenance")
    assert graph.json() == fake.graph_body
    assert pairs.json() == fake.contradictions_body
    assert prov.json() == fake.provenance_body
    # HiddenLinkWindow.from_ must leave as "from", not "from_"
    assert links.json() == fake.links_body
    assert "from" in links.json()[0]["windows"][0]


async def test_retract_sends_operator_actor_and_appends_timeline(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        resp = await client.post(
            f"/api/v1/investigations/{inv}/evidence/ev-1/retract", json={"reason": "forged"}
        )
        assert resp.status_code == 201
        assert resp.json()["finding_ids"] == ["fnd_1", "fnd_2"]
        [(sent_inv, sent_ev, actor, body)] = fake.retracts
        assert (sent_inv, sent_ev) == (inv, "ev-1")
        assert actor == "operator:demo"
        assert fake.requests[-1].headers[ACTOR_HEADER.lower()] == "operator:demo"
        assert body == {"reason": "forged"}

        [event] = _retracted(await _timeline(client, inv))
        assert EvidenceRetractedPayload.model_validate(event.payload) == EvidenceRetractedPayload(
            evidence_id="ev-1", decision_id="dec-1", findings=2, assertions=1, assessments=1
        )
        # counts only, never ids of what was invalidated
        assert set(event.payload) == {
            "evidence_id",
            "decision_id",
            "findings",
            "assertions",
            "assessments",
        }


async def test_retract_without_body_is_forwarded(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        resp = await client.post(f"/api/v1/investigations/{inv}/evidence/ev-1/retract")
    assert resp.status_code == 201
    assert fake.retracts[0][3] in (None, {"reason": None}, {})


async def test_retract_again_no_second_event(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        first = await client.post(f"/api/v1/investigations/{inv}/evidence/ev-1/retract")
        again = await client.post(f"/api/v1/investigations/{inv}/evidence/ev-1/retract")
        assert (first.status_code, again.status_code) == (201, 200)
        assert again.json() == first.json()
        assert len(_retracted(await _timeline(client, inv))) == 1
        assert len(fake.retracts) == 2


async def test_retract_timeline_failure_still_returns_201(app, fake, monkeypatch) -> None:
    async def boom(*_a, **_k):
        raise RuntimeError("disk full")

    async with running(app) as client:
        inv = await _create(client)
        monkeypatch.setattr(runtime(app).repo, "append_retraction_event", boom)
        resp = await client.post(f"/api/v1/investigations/{inv}/evidence/ev-1/retract")
        assert resp.status_code == 201  # committed in the Context Service; timeline is a projection
        assert _retracted(await _timeline(client, inv)) == []


async def test_retract_unknown_investigation_never_reaches_context_service(app, fake) -> None:
    async with running(app) as client:
        resp = await client.post("/api/v1/investigations/inv_NOPE/evidence/ev-1/retract")
    assert resp.status_code == 404
    assert fake.retracts == []


async def test_retract_context_error_appends_nothing(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        fake.proxy_status = 404
        resp = await client.post(f"/api/v1/investigations/{inv}/evidence/ev-9/retract")
        assert resp.status_code == 404
        assert _retracted(await _timeline(client, inv)) == []


async def test_assessment_404_when_none(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        assert fake.assessment is None
        resp = await client.get(f"/api/v1/investigations/{inv}/assessment")
        assert resp.status_code == 404
        fake.assessment = {**ASSESSMENT, "investigation_id": inv}
        ok = await client.get(f"/api/v1/investigations/{inv}/assessment")
        assert ok.status_code == 200 and ok.json()["assessment_id"] == "asm_1"


async def test_assessment_404_when_not_registered_with_context_service(make_app) -> None:
    fake = FakeContextService(up=False)
    app = make_app(fake, context_retry_interval_seconds=60)
    async with running(app) as client:
        inv = await _create(client)
        fake.up = True
        resp = await client.get(f"/api/v1/investigations/{inv}/assessment")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    ("method", "template"),
    [(p.values[0], p.values[1]) for p in ROUTES if "{inv}" in p.values[1]],
)
async def test_unknown_investigation_is_404_without_calling_context_service(
    app, fake, method, template
) -> None:
    async with running(app) as client:
        before = len(fake.requests)
        resp = await client.request(method, "/api/v1" + template.format(inv="inv_NOPE"))
    assert resp.status_code == 404
    assert [r for r in fake.requests[before:] if r.url.path != "/readyz"] == []


@pytest.mark.parametrize(("method", "template"), [(p.values[0], p.values[1]) for p in ROUTES])
async def test_context_down_is_502(app, fake, method, template) -> None:
    async with running(app) as client:
        inv = await _create(client)
        fake.up = False
        resp = await client.request(method, "/api/v1" + template.format(inv=inv))
    assert resp.status_code == 502
    assert resp.json()["code"] == "context_unavailable"


@pytest.mark.parametrize(
    ("method", "template"),
    [(p.values[0], p.values[1]) for p in ROUTES if "assessment" not in p.id],
)
async def test_context_5xx_is_502(app, fake, method, template) -> None:
    async with running(app) as client:
        inv = await _create(client)
        fake.proxy_status = 500
        resp = await client.request(method, "/api/v1" + template.format(inv=inv))
    assert resp.status_code == 502
    assert resp.json()["code"] == "context_unavailable"


async def test_assessment_context_5xx_is_502(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        fake.context_status = 500
        resp = await client.get(f"/api/v1/investigations/{inv}/assessment")
    assert resp.status_code == 502
    assert resp.json()["code"] == "context_unavailable"


async def test_context_service_404_is_404(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        fake.proxy_status = 404
        for path in (
            f"/investigations/{inv}/graph",
            "/assessments/asm_1/provenance",
            f"/investigations/{inv}/links",
            f"/investigations/{inv}/entities/ent-1/contradictions",
        ):
            resp = await client.get("/api/v1" + path)
            assert resp.status_code == 404, path


async def test_other_4xx_passes_through_with_detail(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        fake.proxy_status = 403
        fake.proxy_problem = {
            "title": "Forbidden",
            "status": 403,
            "detail": "caller not allowed",
            "code": "forbidden",
        }
        resp = await client.get(f"/api/v1/investigations/{inv}/graph")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "caller not allowed"
    assert resp.json()["code"] == "forbidden"


async def test_malformed_context_body_is_502(app, fake) -> None:
    async with running(app) as client:
        inv = await _create(client)
        fake.graph_body = {"nodes": "nope"}
        resp = await client.get(f"/api/v1/investigations/{inv}/graph")
    assert resp.status_code == 502
    assert resp.json()["code"] == "context_unavailable"


def test_evidence_retracted_event_rejects_a_payload_missing_a_field() -> None:
    full = {
        "evidence_id": "ev-1",
        "decision_id": "dec-1",
        "findings": 2,
        "assertions": 1,
        "assessments": 1,
    }
    kwargs = {
        "seq": 1,
        "investigation_id": "inv_x",
        "type": TimelineEventType.EVIDENCE_RETRACTED,
        "created_at": utcnow(),
    }
    TimelineEvent(**kwargs, payload=full)
    for missing in full:
        with pytest.raises(ValidationError):
            TimelineEvent(**kwargs, payload={k: v for k, v in full.items() if k != missing})
