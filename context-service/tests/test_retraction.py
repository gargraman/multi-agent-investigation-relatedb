"""``POST /v1/investigations/{id}/evidence/{evidence_id}/retract``: I2 evidence retraction
(S4a FR-216, AC16-s, Review Focus 4, G5).

Reuses the Task 5 fixtures (analysis token injected into the registry, seeded investigations,
retrieve + submit helpers) and the Task 6 INFERENCE seeding."""

from typing import Any

import httpx
from fastapi import FastAPI

from nexus_contracts.api.problem import ProblemDetails
from nexus_contracts.api.retraction import RetractionImpact

from . import test_assessments as base
from .conftest import ORCH, SIGINT, batch_body, finding, read
from .test_assessment_edges import seed_inference
from .test_assessments import (
    ANALYSIS_POST,
    INV,
    INV_B,
    URL,
    Seeded,
    _post_ok,
    assessment_body,
    retrieve,
)

# Fixture objects re-exported by assignment (not import-by-name) so pytest finds them here
# without ruff seeing a redefinition.
app = base.app
client = base.client
seeded = base.seeded

EVIDENCE = "ev-sig-003"  # supports ``seeded.low`` only
OPERATOR = {**ORCH, "X-Nexus-Actor": "operator:demo"}


def retract_url(inv: str = INV, evidence: str = EVIDENCE) -> str:
    return f"/v1/investigations/{inv}/evidence/{evidence}/retract"


async def derive(app: FastAPI, src: str, tgt: str) -> None:
    """``src DERIVED_FROM tgt`` by direct Cypher (the edge an accepted assessment would write)."""
    await app.state.lane.run(
        "test.derive",
        lambda conn: conn.execute(
            "MATCH (s:Finding {id:$s}), (t:Finding {id:$t}) CREATE (s)-[:DERIVED_FROM]->(t)",
            {"s": src, "t": tgt},
        ),
    )


async def statuses(app: FastAPI, label: str, ids: list[str]) -> dict[str, tuple[str, Any]]:
    """``{id: (status, version)}``; assessments have no version (None)."""
    rows = await read(
        app,
        f"MATCH (n:{label}) WHERE n.id IN $ids RETURN n.id AS id, n.status AS status, "
        "n.version AS version",
        {"ids": ids},
    )
    return {r["id"]: (r["status"], r["version"]) for r in rows}


async def evidence_status(app: FastAPI, inv: str = INV, evidence: str = EVIDENCE) -> str:
    [row] = await read(
        app, "MATCH (e:Evidence {uid:$uid}) RETURN e.status AS s", {"uid": f"{inv}:{evidence}"}
    )
    return row["s"]


async def assertion_ids(app: FastAPI, fids: list[str]) -> list[str]:
    rows = await read(
        app,
        "MATCH (f:Finding)-[:ASSERTS]->(s:Assertion) WHERE f.id IN $ids RETURN s.id AS id",
        {"ids": fids},
    )
    return sorted(r["id"] for r in rows)


async def counts(app: FastAPI) -> dict[str, int]:
    [row] = await read(
        app,
        "OPTIONAL MATCH (d:Decision) WITH count(d) AS decisions "
        "OPTIONAL MATCH (a:AuditEvent {operation:'evidence.retract', authz:'ALLOWED'}) "
        "RETURN decisions, count(a) AS audits",
    )
    return row


async def chain(client: httpx.AsyncClient, app: FastAPI, seeded: Seeded) -> list[str]:
    """F1 = ``seeded.low`` (SUPPORTED_BY ev-sig-003) <- F2 <- F3 by DERIVED_FROM; F4 is
    unrelated. Returns ``[f1, f2, f3, f4]``."""
    f2, f3, f4 = await seed_inference(client)
    await derive(app, f2, seeded.low)
    await derive(app, f3, f2)
    return [seeded.low, f2, f3, f4]


# --- AC16-s -------------------------------------------------------------------------------------


async def test_retraction_cascades_any_depth(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    f1, f2, f3, f4 = await chain(client, app, seeded)
    await retrieve(client)
    resp = await _post_ok(client, URL, assessment_body([f3]), ANALYSIS_POST)
    asm = resp["assessment_id"]
    # control: an assessment on the unrelated finding (a second task) must stay ACTIVE
    await retrieve(client, task="tsk_control")
    ctrl = await _post_ok(client, URL, assessment_body([f4], task="tsk_control"), ANALYSIS_POST)

    r = await client.post(retract_url(), json={"reason": "tampered"}, headers=OPERATOR)
    assert r.status_code == 201, r.text
    impact = RetractionImpact.model_validate(r.json())
    assert impact.evidence_id == EVIDENCE
    assert impact.finding_ids == sorted([f1, f2, f3])
    assert impact.assessment_ids == [asm]
    # F1's own assertions are listed (and those of the derived findings with them)
    f1_assertions = await assertion_ids(app, [f1])
    assert f1_assertions and set(f1_assertions) <= set(impact.assertion_ids)
    assert impact.assertion_ids == await assertion_ids(app, [f1, f2, f3])

    assert await evidence_status(app) == "RETRACTED"
    got = await statuses(app, "Finding", [f1, f2, f3, f4])
    assert [got[f][0] for f in (f1, f2, f3, f4)] == ["NEEDS_REVIEW"] * 3 + ["ACTIVE"]
    assert [got[f][1] for f in (f1, f2, f3, f4)] == [2, 2, 2, 1]  # changed status: version + 1
    asms = await statuses(app, "Assessment", [asm, ctrl["assessment_id"]])
    assert asms[asm][0] == "NEEDS_REVIEW"
    assert asms[ctrl["assessment_id"]][0] == "ACTIVE"

    # the Decision: actor from the header, DISPOSES the evidence, holds the impact
    [d] = await read(
        app,
        "MATCH (d:Decision)-[:DISPOSES]->(e:Evidence) "
        "RETURN d.id AS id, d.actor AS actor, d.reason AS reason, e.uid AS uid",
    )
    assert d["id"] == impact.decision_id and d["id"].startswith("dec_")
    assert d["actor"] == "operator:demo" and d["reason"] == "tampered"
    assert d["uid"] == f"{INV}:{EVIDENCE}"
    # one audit event, actor = the operator, CONCERNS the Decision
    [a] = await read(
        app,
        "MATCH (a:AuditEvent {operation:'evidence.retract'})-[:CONCERNS]->(d:Decision) "
        "RETURN a.actor AS actor, a.caller AS caller, a.authz AS authz, a.outcome AS outcome, "
        "d.id AS did",
    )
    assert a == {
        "actor": "operator:demo",
        "caller": "orchestrator",
        "authz": "ALLOWED",
        "outcome": "SUCCESS",
        "did": impact.decision_id,
    }


async def test_retraction_status_rules(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """Already NEEDS_REVIEW: listed, version untouched. SUPERSEDED: never changed, never listed."""
    f1, f2, f3, _ = await chain(client, app, seeded)
    await app.state.lane.run(
        "test.status",
        lambda conn: conn.execute(
            "MATCH (f:Finding) WHERE f.id IN $ids "
            "SET f.status = CASE f.id WHEN $nr THEN 'NEEDS_REVIEW' ELSE 'SUPERSEDED' END",
            {"ids": [f2, f3], "nr": f2},
        ),
    )
    r = await client.post(retract_url(), headers=OPERATOR)
    assert r.status_code == 201, r.text
    impact = RetractionImpact.model_validate(r.json())
    assert impact.finding_ids == sorted([f1, f2])
    got = await statuses(app, "Finding", [f1, f2, f3])
    assert got[f1] == ("NEEDS_REVIEW", 2)
    assert got[f2] == ("NEEDS_REVIEW", 1)
    assert got[f3] == ("SUPERSEDED", 1)
    assert impact.assertion_ids == await assertion_ids(app, [f1, f2])


async def test_retraction_without_dependents(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """Evidence nothing rests on: retracted, empty impact, still a Decision; no actor header
    means ``operator:unknown``."""
    r = await client.post(retract_url(evidence="ev-sig-006"), headers=ORCH)
    assert r.status_code == 201, r.text
    impact = RetractionImpact.model_validate(r.json())
    assert (impact.finding_ids, impact.assertion_ids, impact.assessment_ids) == ([], [], [])
    assert await evidence_status(app, evidence="ev-sig-006") == "RETRACTED"
    [d] = await read(app, "MATCH (d:Decision) RETURN d.actor AS actor, d.reason AS reason")
    assert d == {"actor": "operator:unknown", "reason": None}


# --- idempotency --------------------------------------------------------------------------------


async def test_retraction_is_idempotent(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    f1, f2, f3, _ = await chain(client, app, seeded)
    first = await client.post(retract_url(), headers=OPERATOR)
    assert first.status_code == 201, first.text
    # state moves on after the retraction; a replay must still return the stored impact
    await app.state.lane.run(
        "test.status",
        lambda conn: conn.execute("MATCH (f:Finding {id:$id}) SET f.status = 'ACTIVE'", {"id": f3}),
    )
    snapshot = await counts(app)
    assert snapshot == {"decisions": 1, "audits": 1}
    second = await client.post(retract_url(), headers={**ORCH, "X-Nexus-Actor": "operator:other"})
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert await counts(app) == snapshot  # one Decision, no new audit event
    assert (await statuses(app, "Finding", [f3]))[f3] == ("ACTIVE", 2)  # replay rewrote nothing
    [d] = await read(app, "MATCH (d:Decision) RETURN d.actor AS actor")
    assert d["actor"] == "operator:demo"


# --- scope --------------------------------------------------------------------------------------


async def test_retraction_scoped_to_investigation(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """Review Focus 4: the same manifest evidence id exists in both investigations."""
    [fb] = (
        await _post_ok(
            client,
            "/v1/findings:batch",
            batch_body(
                INV_B,
                task_id="tsk_sig_b",
                findings=[finding("HDL-BRAVO-1", "HDL-DELTA-1", ("ev-sig-003",), day=4)],
            ),
            SIGINT,
        )
    )["finding_ids"]
    r = await client.post(retract_url(INV), headers=OPERATOR)
    assert r.status_code == 201, r.text
    assert RetractionImpact.model_validate(r.json()).finding_ids == [seeded.low]
    assert await evidence_status(app, INV_B) == "ACTIVE"
    assert (await statuses(app, "Finding", [fb]))[fb] == ("ACTIVE", 1)
    # retracting in B is its own, first-time retraction
    rb = await client.post(retract_url(INV_B), headers=OPERATOR)
    assert rb.status_code == 201, rb.text
    assert RetractionImpact.model_validate(rb.json()).finding_ids == [fb]
    assert (await counts(app))["decisions"] == 2


# --- errors and policy --------------------------------------------------------------------------


async def test_retraction_unknown_evidence_404(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    before = await counts(app)
    r = await client.post(retract_url(evidence="ev-nope-999"), headers=OPERATOR)
    assert r.status_code == 404, r.text
    # a manifest id that exists only in another investigation's namespace is unknown here too
    r = await client.post(retract_url("inv_does_not_exist"), headers=OPERATOR)
    assert r.status_code == 404, r.text
    assert await counts(app) == before


async def test_retraction_allowed_after_close(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    c = await client.post(
        f"/v1/investigations/{INV}/close", json={"status": "COMPLETED"}, headers=ORCH
    )
    assert c.status_code == 200, c.text
    r = await client.post(retract_url(), headers=OPERATOR)
    assert r.status_code == 201, r.text
    assert RetractionImpact.model_validate(r.json()).finding_ids == [seeded.low]
    assert await evidence_status(app) == "RETRACTED"


async def test_actor_header_ignored_from_other_callers(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """Policy: only the orchestrator may retract. Analysis (and the agents) get a 403, audited,
    and nothing changes."""
    for headers in (
        {**ANALYSIS_POST, "X-Nexus-Actor": "operator:demo"},
        {**SIGINT, "X-Nexus-Actor": "operator:demo"},
    ):
        r = await client.post(retract_url(), headers=headers)
        assert r.status_code == 403, r.text
        ProblemDetails.model_validate(r.json())
    assert await evidence_status(app) == "ACTIVE"
    assert await counts(app) == {"decisions": 0, "audits": 0}
    denied = await read(
        app,
        "MATCH (a:AuditEvent {operation:'evidence.retract', authz:'DENIED'}) "
        "RETURN a.caller AS caller, a.actor AS actor ORDER BY a.caller",
    )
    assert denied == [
        {"caller": "analysis-agent", "actor": "analysis-agent"},
        {"caller": "sigint-agent", "actor": "sigint-agent"},
    ]
    # and with no token at all: 401
    assert (await client.post(retract_url())).status_code == 401


async def test_retract_body_is_optional_and_validated(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.post(retract_url(), json={"reason": ""}, headers=OPERATOR)
    assert r.status_code == 422, r.text
    assert await evidence_status(app) == "ACTIVE"
    assert (await client.post(retract_url(), headers=OPERATOR)).status_code == 201


async def test_assessment_citing_retracted_finding_is_needs_review(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """Trust-a through the real retraction path: retrieve, retract, then submit (FR-211a)."""
    await retrieve(client)
    assert (await client.post(retract_url(), headers=OPERATOR)).status_code == 201
    resp = await _post_ok(client, URL, assessment_body([seeded.low]), ANALYSIS_POST)
    assert (await statuses(app, "Assessment", [resp["assessment_id"]]))[resp["assessment_id"]][
        0
    ] == "NEEDS_REVIEW"
