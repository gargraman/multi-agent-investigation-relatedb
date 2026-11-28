"""I1 provenance and I3 contradiction neighbourhood (S4a FR-215, FR-217, AC9-s part 2, G7, §9.5).

Reuses the Task 5 fixtures and helpers (analysis token injected into the registry, seeded
investigation, retrieve + submit helpers). Per G7 there is no "view at clearance" parameter, so
the below-clearance branches are repository-level tests with ``max_rank=2``; the one HTTP test of
the hidden case swaps the app's policy to give the orchestrator a rank-2 read clearance."""

import dataclasses

import httpx
import pytest
from fastapi import FastAPI

from nexus_context_service.repository import AssessmentNotFound, EntityNotFound
from nexus_contracts.api.assessments import AssessmentSubmissionResponse
from nexus_contracts.api.graph import ContradictionPair, Provenance
from nexus_contracts.api.problem import ProblemDetails
from nexus_contracts.common import Classification

from . import test_assessments as base
from .conftest import ORCH, SIGINT, read
from .test_assessment_edges import edge, submit
from .test_assessments import (
    ANALYSIS_POST,
    INV,
    INV_B,
    URL,
    Seeded,
    _post_ok,
    _set_assessment,
    assessment_body,
    retrieve,
)

# Fixture objects re-exported by assignment (not import-by-name) so pytest finds them here
# without ruff seeing a redefinition.
app = base.app
client = base.client
seeded = base.seeded

OPERATOR = {**ORCH, "X-Nexus-Actor": "operator:demo"}


def provenance_url(assessment_id: str) -> str:
    return f"/v1/assessments/{assessment_id}/provenance"


def contradictions_url(entity: str, inv: str = INV) -> str:
    return f"/v1/investigations/{inv}/entities/{entity}/contradictions"


async def assess(client: httpx.AsyncClient, supporting: list[str], task: str = base.TASK) -> str:
    await retrieve(client, task=task)
    resp = await _post_ok(client, URL, assessment_body(supporting, task=task), ANALYSIS_POST)
    return resp["assessment_id"]


async def audits(app: FastAPI, operation: str) -> list[dict]:
    return await read(
        app,
        "MATCH (a:AuditEvent {operation:$op}) RETURN a.authz AS authz, a.outcome AS outcome, "
        "a.target_id AS target_id ORDER BY a.at",
        {"op": operation},
    )


# --- I1 provenance ------------------------------------------------------------------------------


async def test_provenance_trail(client: httpx.AsyncClient, app: FastAPI, seeded: Seeded) -> None:
    # low (SIGINT, DEMO-LOW) is supported by ev-sig-003; f1 (GEOINT, DEMO-MID) by ev-geo-001.
    asm = await assess(client, [seeded.low, seeded.f1])
    other = await assess(client, [seeded.f2], task="tsk_other")  # must not leak into the trail

    r = await client.get(provenance_url(asm), headers=ORCH)
    assert r.status_code == 200, r.text
    prov = Provenance.model_validate(r.json())
    assert prov.assessment_id == asm
    assert [f.finding_id for f in prov.findings] == sorted([seeded.low, seeded.f1])
    by_id = {f.finding_id: f for f in prov.findings}
    low, geo = by_id[seeded.low], by_id[seeded.f1]
    assert (low.source_agent_id, low.status, low.classification) == (
        "sigint-agent",
        "ACTIVE",
        Classification.DEMO_LOW,
    )
    assert [e.model_dump() for e in low.evidence] == [
        {
            "evidence_id": "ev-sig-003",
            "sha256": low.evidence[0].sha256,
            "owner_node_id": "sigint-agent",
            "status": "ACTIVE",
        }
    ]
    assert len(low.evidence[0].sha256) == 64
    assert (geo.source_agent_id, geo.classification) == ("geoint-agent", Classification.DEMO_MID)
    assert [(e.evidence_id, e.owner_node_id, e.status) for e in geo.evidence] == [
        ("ev-geo-001", "geoint-agent", "ACTIVE")
    ]

    # Retract E1: the trail keeps the finding, evidence RETRACTED and finding NEEDS_REVIEW.
    rr = await client.post(
        f"/v1/investigations/{INV}/evidence/ev-sig-003/retract",
        json={"reason": "tampered"},
        headers=OPERATOR,
    )
    assert rr.status_code == 201, rr.text
    after = Provenance.model_validate((await client.get(provenance_url(asm), headers=ORCH)).json())
    assert [f.finding_id for f in after.findings] == [f.finding_id for f in prov.findings]
    low2 = next(f for f in after.findings if f.finding_id == seeded.low)
    assert low2.status == "NEEDS_REVIEW"
    assert [e.status for e in low2.evidence] == ["RETRACTED"]
    geo2 = next(f for f in after.findings if f.finding_id == seeded.f1)
    assert geo2.status == "ACTIVE" and [e.status for e in geo2.evidence] == ["ACTIVE"]
    # the other assessment's trail is its own
    o = Provenance.model_validate((await client.get(provenance_url(other), headers=ORCH)).json())
    assert [f.finding_id for f in o.findings] == [seeded.f2]
    # a read writes no audit event (not in FR-204's list)
    assert await audits(app, "assessments.provenance") == []


async def test_provenance_unknown_404(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.get(provenance_url("asm_doesnotexist"), headers=ORCH)
    assert r.status_code == 404
    ProblemDetails.model_validate(r.json())
    assert await audits(app, "assessments.provenance") == []  # unknown is not a denial


async def test_provenance_requires_orchestrator(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    asm = await assess(client, [seeded.f1])
    assert (await client.get(provenance_url(asm), headers=ANALYSIS_POST)).status_code == 403
    assert (await client.get(provenance_url(asm), headers=SIGINT)).status_code == 403
    assert (await client.get(provenance_url(asm))).status_code == 401


async def test_provenance_hidden_is_404_at_lower_rank(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    asm = await assess(client, [seeded.f1])
    await _set_assessment(app, asm, "classification_rank", 3)
    repo = app.state.repository
    with pytest.raises(AssessmentNotFound):
        await repo.provenance(asm, 2)
    with pytest.raises(AssessmentNotFound):
        await repo.provenance("asm_doesnotexist", 2)
    assert (await repo.provenance(asm, 3)).assessment_id == asm  # visible at its own rank
    # findings above the caller's rank are dropped from a visible assessment's trail
    await _set_assessment(app, asm, "classification_rank", 2)
    await app.state.lane.run(
        "test.rank",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.classification_rank = 3", {"id": seeded.f1}
        ),
    )
    assert (await repo.provenance(asm, 2)).findings == []
    assert [f.finding_id for f in (await repo.provenance(asm, 3)).findings] == [seeded.f1]


async def test_provenance_hidden_http_is_same_404_plus_denied_audit(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    asm = await assess(client, [seeded.f1])
    await _set_assessment(app, asm, "classification_rank", 3)
    unknown = await client.get(provenance_url("asm_doesnotexist"), headers=ORCH)
    # P0 has no way to read the orchestrator at rank 2 (G7): swap the policy for this test only.
    callers = dict(app.state.policy.callers)
    callers["orchestrator"] = dataclasses.replace(
        callers["orchestrator"], read=Classification.DEMO_MID
    )
    app.state.policy = dataclasses.replace(app.state.policy, callers=callers)
    hidden = await client.get(provenance_url(asm), headers=ORCH)
    assert hidden.status_code == unknown.status_code == 404
    # same body apart from the echoed request path: existence is not revealed
    assert {**hidden.json(), "instance": None} == {**unknown.json(), "instance": None}
    rows = await audits(app, "assessments.provenance")
    assert [(r["authz"], r["outcome"]) for r in rows] == [("DENIED", "DENIED")]
    assert rows[0]["target_id"] == INV  # the investigation, never the hidden record's id


# --- I3 contradictions --------------------------------------------------------------------------


async def test_contradictions_pair_once(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    # f1 and f2 are both ABOUT LOC-GRID-00; the edge is stored f2 -> f1 so ordering is tested.
    resp = await submit(
        client, [seeded.f1, seeded.f2], [edge("CONTRADICTS", seeded.f2, seeded.f1)], base.TASK
    )
    assert isinstance(resp, AssessmentSubmissionResponse) and resp.rejected_edges == []
    lo, hi = sorted([seeded.f1, seeded.f2])

    r = await client.get(contradictions_url("LOC-GRID-00"), headers=ORCH)
    assert r.status_code == 200, r.text
    pairs = [ContradictionPair.model_validate(p) for p in r.json()]
    assert [(p.finding_a, p.finding_b) for p in pairs] == [(lo, hi)]  # once, lower id first

    # both directions stored: still one pair
    await app.state.lane.run(
        "test.reverse",
        lambda conn: conn.execute(
            "MATCH (a:Finding {id:$a}), (b:Finding {id:$b}) CREATE (a)-[:CONTRADICTS]->(b)",
            {"a": lo, "b": hi},
        ),
    )
    r = await client.get(contradictions_url("LOC-GRID-00"), headers=ORCH)
    assert [(p["finding_a"], p["finding_b"]) for p in r.json()] == [(lo, hi)]

    # an entity only one of them is ABOUT, and one nobody contradicts: empty (not 404)
    assert (await client.get(contradictions_url("ENT-ALPHA"), headers=ORCH)).json() == []
    assert (await client.get(contradictions_url("ENT-BRAVO"), headers=ORCH)).json() == []
    assert await audits(app, "entities.contradictions") == []


async def test_contradictions_unknown_entity_or_investigation_404(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    assert (await client.get(contradictions_url("ENT-NOPE"), headers=ORCH)).status_code == 404
    assert (
        await client.get(contradictions_url("LOC-GRID-00", "inv_nope"), headers=ORCH)
    ).status_code == 404
    assert (
        await client.get(contradictions_url("LOC-GRID-00"), headers=ANALYSIS_POST)
    ).status_code == 403


async def test_contradictions_scoped_to_investigation(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await submit(
        client, [seeded.f1, seeded.f2], [edge("CONTRADICTS", seeded.f1, seeded.f2)], base.TASK
    )
    # the same manifest entity id in investigation B has no contradictions
    r = await client.get(contradictions_url("LOC-GRID-00", INV_B), headers=ORCH)
    assert r.status_code == 200 and r.json() == []


async def test_contradictions_hide_high_at_lower_rank(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await submit(
        client, [seeded.f1, seeded.f2], [edge("CONTRADICTS", seeded.f1, seeded.f2)], base.TASK
    )
    lo, hi = sorted([seeded.f1, seeded.f2])
    repo = app.state.repository
    assert [
        (p.finding_a, p.finding_b) for p in await repo.contradictions(INV, "LOC-GRID-00", 2)
    ] == [(lo, hi)]
    await app.state.lane.run(
        "test.rank",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.classification_rank = 3", {"id": hi}
        ),
    )
    assert await repo.contradictions(INV, "LOC-GRID-00", 2) == []  # one hidden member: no pair
    assert len(await repo.contradictions(INV, "LOC-GRID-00", 3)) == 1
    with pytest.raises(EntityNotFound):
        await repo.contradictions(INV, "ENT-NOPE", 3)
