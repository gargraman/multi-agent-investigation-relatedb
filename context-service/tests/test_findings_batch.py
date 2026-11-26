"""POST /v1/findings:batch and GET /v1/investigations/{id}/context (S2 FR-040–FR-045).

Real embedded Relate store per test (tmp path); nothing is mocked."""

import copy
from typing import Any

import pytest
from fastapi import FastAPI

from nexus_context_service.queries import findings as fq
from nexus_contracts.api.context import InvestigationContext
from nexus_contracts.api.findings import (
    AlreadyCommittedProblem,
    FindingBatchRequest,
    FindingBatchResponse,
)
from nexus_contracts.api.problem import PROBLEM_JSON_MEDIA_TYPE, ProblemDetails

from .conftest import (
    GEOINT,
    ORCH,
    SIGINT,
    batch_body,
    client_for,
    create_body,
    evidence_ref,
    finding,
    read,
)

URL = "/v1/findings:batch"


def _problem(resp, status: int, code: str | None = None) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    body = resp.json()
    ProblemDetails.model_validate(body)
    if code is not None:
        assert body["code"] == code
    return body


async def _create(client, inv: str) -> None:
    r = await client.post("/v1/investigations", json=create_body(inv), headers=ORCH)
    assert r.status_code == 201, r.text


async def _counts(app: FastAPI) -> dict[str, int]:
    out = {}
    for label in ("Finding", "Submission", "Assertion", "AuditEvent"):
        [row] = await read(app, f"MATCH (n:{label}) RETURN count(n) AS c")
        out[label] = row["c"]
    [row] = await read(app, "MATCH ()-[r]->() RETURN count(r) AS c")
    out["edges"] = row["c"]
    return out


# --- success, provenance, audit --------------------------------------------------------------


async def test_commit_201_writes_graph_with_server_provenance(client, app: FastAPI) -> None:
    await _create(client, "inv_ok")
    before = await _counts(app)
    r = await client.post(URL, json=batch_body("inv_ok"), headers=SIGINT)
    assert r.status_code == 201, r.text
    resp = FindingBatchResponse.model_validate(r.json())
    assert resp.task_id == "tsk_one"
    assert len(resp.finding_ids) == 2
    assert all(fid.startswith("fnd_") for fid in resp.finding_ids)
    assert resp.finding_ids == sorted(resp.finding_ids)  # monotonic, submission order

    rows = await read(
        app,
        "MATCH (s:Submission {idempotency_key:'findings:tsk_one'})-[:COMMITTED]->(f:Finding) "
        "MATCH (:Investigation {id:'inv_ok'})-[:CONTAINS]->(f) "
        "RETURN f.id AS id, f.source_node_id AS node, f.source_agent_id AS agent, "
        "f.status AS status, f.task_id AS task, f.attempt AS attempt, f.version AS version, "
        "f.classification_rank AS rank, f.submission_id AS sub, s.id AS sid, "
        "f.created_at AS created, f.valid_from AS vf ORDER BY f.id",
    )
    assert [r["id"] for r in rows] == resp.finding_ids
    for row in rows:
        assert row["node"] == row["agent"] == "sigint-agent"  # AC5-b, G19
        assert (row["status"], row["task"], row["attempt"], row["version"]) == (
            "ACTIVE",
            "tsk_one",
            1,
            1,
        )
        assert row["rank"] == 1
        assert row["sub"] == row["sid"] and row["sid"].startswith("sub_")
        assert row["created"] is not None
        assert hasattr(row["vf"], "tzinfo")  # temporal, not a string

    # Assertions: datetimes stored as temporal values (G16) and comparable.
    [n] = await read(
        app,
        "MATCH (a:Assertion {type:'LINKED_TO'})-[:SUBJECT]->(:Entity {uid:'inv_ok:HDL-ALPHA-1'}) "
        "WHERE a.valid_from >= datetime('2026-11-03T12:00:00Z') RETURN count(a) AS c",
    )
    assert n["c"] == 1
    [edges] = await read(
        app,
        "MATCH (f:Finding {id:$fid}) RETURN COUNT { (f)-[:ABOUT]->(:Entity) } AS about, "
        "COUNT { (f)-[:SUPPORTED_BY]->(:Evidence) } AS ev, "
        "COUNT { (f)-[:ASSERTS]->(:Assertion) } AS asr",
        {"fid": resp.finding_ids[0]},
    )
    assert edges == {"about": 2, "ev": 2, "asr": 2}
    # G17: the summary is on the edge and copied onto the Evidence node.
    [ev] = await read(
        app,
        "MATCH (:Finding)-[r:SUPPORTED_BY]->(e:Evidence {uid:'inv_ok:ev-sig-001'}) "
        "RETURN r.summary AS edge, e.summary AS node, r.sha256 AS sha",
    )
    assert ev["edge"] == ev["node"] == "Contact metadata from ev-sig-001."

    after = await _counts(app)
    assert after["AuditEvent"] - before["AuditEvent"] == 1
    # G18: audit CONCERNS the Submission.
    [audit] = await read(
        app,
        "MATCH (a:AuditEvent {operation:'findings.submit'})-[:CONCERNS]->(s:Submission) "
        "RETURN a.caller AS caller, a.outcome AS outcome, a.target_id AS target, s.id AS sid",
    )
    assert audit["caller"] == "sigint-agent" and audit["outcome"] == "SUCCESS"
    assert audit["target"] == audit["sid"]


async def test_evidence_owner_is_agent_id(client, app: FastAPI) -> None:
    """G21: S1 stores Evidence.owner_node_id as the AgentId value, matching the caller id."""
    await _create(client, "inv_own")
    owners = await read(
        app,
        "MATCH (e:Evidence {investigation_id:'inv_own'}) RETURN DISTINCT e.owner_node_id AS o",
    )
    assert {r["o"] for r in owners} == {"sigint-agent", "geoint-agent"}


# --- idempotency (FR-040, AC5-c) ---------------------------------------------------------------


async def test_replay_reorder_and_conflict(client, app: FastAPI) -> None:
    await _create(client, "inv_idem")
    body = batch_body("inv_idem")
    r1 = await client.post(URL, json=body, headers=SIGINT)
    assert r1.status_code == 201
    ids = r1.json()["finding_ids"]
    snapshot = await _counts(app)

    r2 = await client.post(URL, json=body, headers=SIGINT)
    assert r2.status_code == 200
    assert r2.json() == {"task_id": "tsk_one", "finding_ids": ids}

    # Reordered findings, evidence, assertions and a swapped CONTACT pair: same content hash.
    reordered = copy.deepcopy(body)
    reordered["findings"].reverse()
    for f in reordered["findings"]:
        f["evidence"].reverse()
        f["about"].reverse()
        f["assertions"].reverse()
        for a in f["assertions"]:
            a["subject"], a["object"] = a["object"], a["subject"]
    reordered["attempt"] = 2  # a later attempt with identical content is still a replay
    assert (
        FindingBatchRequest.model_validate(reordered).content_hash()
        == FindingBatchRequest.model_validate(body).content_hash()
    )
    r3 = await client.post(URL, json=reordered, headers=SIGINT)
    assert r3.status_code == 200, r3.text
    assert r3.json()["finding_ids"] == ids

    changed = copy.deepcopy(body)
    changed["findings"][0]["statement"] = "A different statement."
    r4 = await client.post(URL, json=changed, headers=SIGINT)
    problem = AlreadyCommittedProblem.model_validate(_problem(r4, 409, "already_committed"))
    assert problem.finding_ids == ids

    # Replays and the conflict wrote nothing: one audit event in total (AC5-c).
    assert await _counts(app) == snapshot
    [a] = await read(app, "MATCH (a:AuditEvent {operation:'findings.submit'}) RETURN count(a) AS c")
    assert a["c"] == 1


async def test_same_task_id_from_other_investigation_conflicts(client) -> None:
    await _create(client, "inv_x1")
    await _create(client, "inv_x2")
    r1 = await client.post(URL, json=batch_body("inv_x1"), headers=SIGINT)
    assert r1.status_code == 201
    r2 = await client.post(URL, json=batch_body("inv_x2"), headers=SIGINT)
    body = _problem(r2, 409, "already_committed")
    assert body["finding_ids"] == r1.json()["finding_ids"]


def _geoint_finding(person: str = "ENT-CHARLIE", cell: str = "LOC-GRID-09") -> dict[str, Any]:
    """A valid GEOINT finding: OBSERVATION with one PLACED_AT assertion (DEMO-MID)."""
    f = finding(evidence=("ev-geo-001",), classification="DEMO-MID")
    f["evidence"] = [evidence_ref("ev-geo-001", owner="geoint-agent")]
    f["about"] = [person, cell]
    f["statement"] = f"{person} was placed at {cell}."
    f["prompt_version"] = "geoint-classify@1.0.0"
    f["assertions"] = [
        {
            "type": "PLACED_AT",
            "subject": person,
            "location": cell,
            "valid_from": f["valid_from"],
            "valid_until": f["valid_until"],
        }
    ]
    return f


async def test_geoint_agent_batch_with_placed_at_accepted(client, app: FastAPI) -> None:
    await _create(client, "inv_geo_ok")
    body = batch_body("inv_geo_ok", task_id="tsk_geo", findings=[_geoint_finding()])
    r = await client.post(URL, json=body, headers=GEOINT)
    assert r.status_code == 201, r.text
    [fid] = FindingBatchResponse.model_validate(r.json()).finding_ids
    [row] = await read(
        app,
        "MATCH (f:Finding {id:$fid})-[:ASSERTS]->(a:Assertion {type:'PLACED_AT'}) "
        "RETURN f.source_agent_id AS agent, a.type AS type",
        {"fid": fid},
    )
    assert row == {"agent": "geoint-agent", "type": "PLACED_AT"}
    [audit] = await read(
        app, "MATCH (a:AuditEvent {operation:'findings.submit'}) RETURN a.caller AS caller"
    )
    assert audit["caller"] == "geoint-agent"


async def test_same_task_id_from_other_caller_is_403_without_ids(client, app: FastAPI) -> None:
    await _create(client, "inv_scope")
    r1 = await client.post(URL, json=batch_body("inv_scope", task_id="tsk_X"), headers=SIGINT)
    assert r1.status_code == 201, r1.text
    snapshot = await _counts(app)

    geo_body = batch_body("inv_scope", task_id="tsk_X", findings=[_geoint_finding()])
    r2 = await client.post(URL, json=geo_body, headers=GEOINT)
    problem = _problem(r2, 403, "forbidden")
    assert "fnd_" not in r2.text and "finding_ids" not in problem
    assert "sigint" not in r2.text.lower()
    # Even an identical body from the other caller is refused (the key is caller-scoped).
    r3 = await client.post(URL, json=batch_body("inv_scope", task_id="tsk_X"), headers=GEOINT)
    _problem(r3, 403, "forbidden")
    assert "fnd_" not in r3.text
    assert await _counts(app) == snapshot  # nothing written

    # The owner can still replay.
    r4 = await client.post(URL, json=batch_body("inv_scope", task_id="tsk_X"), headers=SIGINT)
    assert r4.status_code == 200 and r4.json()["finding_ids"] == r1.json()["finding_ids"]


async def test_same_caller_replay_still_200(client) -> None:
    await _create(client, "inv_geo_replay")
    body = batch_body("inv_geo_replay", task_id="tsk_geo_r", findings=[_geoint_finding()])
    r1 = await client.post(URL, json=body, headers=GEOINT)
    assert r1.status_code == 201, r1.text
    r2 = await client.post(URL, json=body, headers=GEOINT)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()


# --- fence (FR-042, G15) ---------------------------------------------------------------------


async def test_cancelled_fence_and_replay_before_fence(client, app: FastAPI) -> None:
    await _create(client, "inv_f")
    body = batch_body("inv_f")
    r1 = await client.post(URL, json=body, headers=SIGINT)
    assert r1.status_code == 201
    close = await client.post(
        "/v1/investigations/inv_f/close", json={"status": "CANCELLED"}, headers=ORCH
    )
    assert close.status_code == 200
    snapshot = await _counts(app)

    # Replay after close still learns its committed ids (G15).
    r2 = await client.post(URL, json=body, headers=SIGINT)
    assert r2.status_code == 200
    assert r2.json()["finding_ids"] == r1.json()["finding_ids"]
    # A new task after close is fenced.
    r3 = await client.post(URL, json=batch_body("inv_f", task_id="tsk_two"), headers=SIGINT)
    _problem(r3, 409, "investigation_cancelled")
    assert await _counts(app) == snapshot


async def test_unknown_investigation_404(client) -> None:
    r = await client.post(URL, json=batch_body("inv_missing"), headers=SIGINT)
    _problem(r, 404)


# --- auth (FR-043, FR-045) -------------------------------------------------------------------


async def test_wrong_caller_403_audited_and_401(client, app: FastAPI) -> None:
    await _create(client, "inv_auth")
    r = await client.post(URL, json=batch_body("inv_auth"), headers=ORCH)
    _problem(r, 403)
    r = await client.post(URL, json=batch_body("inv_auth"))
    _problem(r, 401)
    r = await client.get("/v1/investigations/inv_auth/context", headers=SIGINT)
    _problem(r, 403)
    denials = await read(
        app,
        "MATCH (a:AuditEvent {authz:'DENIED'}) RETURN a.caller AS caller, a.operation AS op "
        "ORDER BY op",
    )
    assert denials == [
        {"caller": "orchestrator", "op": "findings.submit"},
        {"caller": "sigint-agent", "op": "investigations.context"},
    ]
    [f] = await read(app, "MATCH (f:Finding) RETURN count(f) AS c")
    assert f["c"] == 0


# --- FR-041 validation -> 422 ----------------------------------------------------------------


def _mutate(fn) -> dict[str, Any]:
    body = batch_body("inv_val")
    fn(body["findings"][0])
    return body


def _set_evidence(f: dict, **kw: Any) -> None:
    f["evidence"][0] = {**f["evidence"][0], **kw}


CASES = [
    # (name, mutation, expected code, expected loc suffix)
    (
        "about unknown entity",
        lambda f: f.update(about=["HDL-ALPHA-1", "HDL-CHARLIE-1", "HDL-NOPE"]),
        "schema_invalid",
        ["about", 2],
    ),
    (
        "about not a HANDLE",
        lambda f: f.update(about=["HDL-ALPHA-1", "HDL-CHARLIE-1", "ENT-ALPHA"]),
        "schema_invalid",
        ["about", 2],
    ),
    (
        "evidence unknown",
        lambda f: f["evidence"].append({**evidence_ref("ev-sig-001"), "evidence_id": "ev-nope"}),
        "schema_invalid",
        ["evidence", 2, "evidence_id"],
    ),
    (
        "evidence sha mismatch",
        lambda f: _set_evidence(f, sha256="0" * 64),
        "evidence_integrity_error",
        ["evidence", 0, "sha256"],
    ),
    (
        "evidence owned by another agent",
        lambda f: f["evidence"].append(evidence_ref("ev-geo-001")),
        "schema_invalid",
        ["evidence", 2, "evidence_id"],
    ),
    (
        "ref owner_node_id is not the caller",
        lambda f: _set_evidence(f, owner_node_id="geoint-agent"),
        "schema_invalid",
        ["evidence", 0, "owner_node_id"],
    ),
    (
        "finding classification below evidence",
        lambda f: f["evidence"].append(evidence_ref("ev-sig-004")),  # DEMO-MID evidence
        "schema_invalid",
        ["classification"],
    ),
    (
        "assertion object not in about",
        lambda f: f.update(
            about=["HDL-ALPHA-1", "HDL-CHARLIE-1"],
            assertions=[{**f["assertions"][0], "object": "HDL-DELTA-1"}],
        ),
        "schema_invalid",
        ["assertions", 0, "object"],
    ),
    (
        "assertion endpoint not a HANDLE",
        lambda f: f.update(
            about=["ENT-ALPHA", "HDL-ALPHA-1"],
            assertions=[{**f["assertions"][0], "subject": "ENT-ALPHA", "object": "HDL-ALPHA-1"}],
        ),
        "schema_invalid",
        ["assertions", 0, "subject"],
    ),
    (
        "PLACED_AT not allowed for SIGINT",
        lambda f: f.update(
            assertions=[
                {
                    "type": "PLACED_AT",
                    "subject": "HDL-ALPHA-1",
                    "location": "LOC-GRID-00",
                    "valid_from": f["valid_from"],
                    "valid_until": f["valid_until"],
                }
            ]
        ),
        "schema_invalid",
        ["assertions", 0, "type"],
    ),
]


@pytest.mark.parametrize(("name", "mutate", "code", "loc"), CASES, ids=[c[0] for c in CASES])
async def test_fr041_violations_422(client, app: FastAPI, name, mutate, code, loc) -> None:
    await _create(client, "inv_val")
    snapshot = await _counts(app)
    r = await client.post(URL, json=_mutate(mutate), headers=SIGINT)
    problem = _problem(r, 422, code)
    locs = [e["loc"] for e in problem["errors"]]
    assert ["body", "findings", 0, *loc] in locs, locs
    assert await _counts(app) == snapshot  # nothing written, nothing audited
    # The same task can still commit a valid batch afterwards (no Submission was left behind).
    ok = await client.post(URL, json=batch_body("inv_val"), headers=SIGINT)
    assert ok.status_code == 201


async def test_retracted_evidence_rejected(client, app: FastAPI) -> None:
    await _create(client, "inv_ret")
    await app.state.lane.run(
        "test.retract",
        lambda conn: conn.execute(
            "MATCH (e:Evidence {uid:'inv_ret:ev-sig-001'}) SET e.status = 'RETRACTED'"
        ),
    )
    r = await client.post(URL, json=batch_body("inv_ret"), headers=SIGINT)
    problem = _problem(r, 422, "schema_invalid")
    assert ["body", "findings", 0, "evidence", 0, "evidence_id"] in [
        e["loc"] for e in problem["errors"]
    ]


async def test_geoint_clearance_and_rules(client) -> None:
    """GEOINT (S3) is accepted, but clearance (DEMO-MID) and who-asserts-what apply."""
    await _create(client, "inv_geo")
    f = finding(evidence=("ev-geo-001",), classification="DEMO-HIGH")
    f["evidence"] = [evidence_ref("ev-geo-001", owner="geoint-agent")]
    r = await client.post(URL, json=batch_body("inv_geo", findings=[f]), headers=GEOINT)
    problem = _problem(r, 422, "schema_invalid")
    locs = [e["loc"] for e in problem["errors"]]
    assert ["body", "findings", 0, "classification"] in locs
    assert ["body", "findings", 0, "about", 0] in locs  # HANDLE not allowed for GEOINT
    assert ["body", "findings", 0, "assertions", 0, "type"] in locs


async def test_local_schema_violation_422(client) -> None:
    await _create(client, "inv_schema")
    body = batch_body("inv_schema")
    body["findings"][0]["valid_until"] = body["findings"][0]["valid_from"]
    body["findings"][0]["id"] = "fnd_client_set"  # server-set provenance is not accepted
    r = await client.post(URL, json=body, headers=SIGINT)
    _problem(r, 422, "schema_invalid")


# --- atomicity (ADR-001 R2) ------------------------------------------------------------------


async def test_failing_statement_mid_batch_rolls_back(client, app: FastAPI, monkeypatch) -> None:
    await _create(client, "inv_atomic")
    snapshot = await _counts(app)
    # Break the assertion statement (step 6): Submission, Findings, ABOUT and SUPPORTED_BY are
    # already written in the same transaction when it fails.
    monkeypatch.setattr(fq, "CREATE_LINKED_TO", "UNWIND $rows AS r CREATE (:Broken {x: r.nope.")
    async with client_for(app, raise_app_exceptions=False) as c:
        r = await c.post(URL, json=batch_body("inv_atomic"), headers=SIGINT)
    _problem(r, 500)
    assert await _counts(app) == snapshot
    [s] = await read(app, "MATCH (e:Evidence) WHERE e.summary IS NOT NULL RETURN count(e) AS c")
    assert s["c"] == 0
    monkeypatch.undo()
    ok = await client.post(URL, json=batch_body("inv_atomic"), headers=SIGINT)
    assert ok.status_code == 201


# --- context read model (FR-044) -------------------------------------------------------------


async def test_context_read_model(client) -> None:
    await _create(client, "inv_ctx")
    empty = await client.get("/v1/investigations/inv_ctx/context", headers=ORCH)
    assert empty.status_code == 200
    assert InvestigationContext.model_validate(empty.json()).findings == []

    body = batch_body("inv_ctx")
    r1 = await client.post(URL, json=body, headers=SIGINT)
    later = batch_body(
        "inv_ctx",
        task_id="tsk_two",
        findings=[finding("HDL-ECHO-1", "HDL-DELTA-1", ("ev-sig-005", "ev-sig-006"), day=5)],
    )
    r2 = await client.post(URL, json=later, headers=SIGINT)
    assert r1.status_code == r2.status_code == 201

    r = await client.get("/v1/investigations/inv_ctx/context", headers=ORCH)
    assert r.status_code == 200, r.text
    ctx = InvestigationContext.model_validate(r.json())
    assert ctx.investigation_id == "inv_ctx"
    assert ctx.status.value == "CREATED"
    assert [f.id for f in ctx.findings] == r1.json()["finding_ids"] + r2.json()["finding_ids"]

    first = ctx.findings[0]
    sent = FindingBatchRequest.model_validate(body).findings[0]
    assert first.source_node_id.value == "sigint-agent"
    assert (first.task_id, first.attempt, first.status.value) == ("tsk_one", 1, "ACTIVE")
    assert first.about == sorted(sent.about)
    assert [(e.evidence_id, e.sha256, e.summary) for e in first.evidence] == sorted(
        (e.evidence_id, e.sha256, e.summary) for e in sent.evidence
    )
    assert first.assertions == sorted(sent.assertions, key=lambda a: a.sort_key())
    assert (first.valid_from, first.valid_until) == (sent.valid_from, sent.valid_until)
    assert first.statement == sent.statement and first.confidence == sent.confidence
    assert first.model_id == sent.model_id and first.prompt_version == sent.prompt_version
    # Delta (HDL-DELTA-1 < HDL-ECHO-1): canonical CONTACT order survives the round trip.
    last = ctx.findings[-1]
    assert {(a.subject, a.object) for a in last.assertions} == {("HDL-DELTA-1", "HDL-ECHO-1")}


async def test_context_404_and_closed_status(client) -> None:
    r = await client.get("/v1/investigations/inv_none/context", headers=ORCH)
    _problem(r, 404)
    await _create(client, "inv_cs")
    await client.post("/v1/investigations/inv_cs/close", json={"status": "FAILED"}, headers=ORCH)
    r = await client.get("/v1/investigations/inv_cs/context", headers=ORCH)
    assert InvestigationContext.model_validate(r.json()).status.value == "FAILED"
