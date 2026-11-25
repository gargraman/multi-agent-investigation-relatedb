import asyncio

from fastapi import FastAPI

from nexus_contracts.api.problem import PROBLEM_JSON_MEDIA_TYPE
from nexus_contracts.scenario.loader import load_manifest

from .conftest import MANIFEST, ORCH, create_body, read


def _problem(resp, status: int, code: str | None = None) -> dict:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    body = resp.json()
    assert body["status"] == status
    if code is not None:
        assert body["code"] == code
    return body


async def test_create_replay_conflict(client, app: FastAPI) -> None:
    body = create_body("inv_one")
    r1 = await client.post("/v1/investigations", json=body, headers=ORCH)
    assert r1.status_code == 201, r1.text
    assert r1.json()["created"] is True
    manifest = load_manifest(MANIFEST)
    assert r1.json()["entity_count"] == len(manifest.entities)
    assert r1.json()["evidence_count"] == len(manifest.evidence)

    nodes_before = await read(app, "MATCH (n) RETURN count(n) AS c")
    r2 = await client.post("/v1/investigations", json=body, headers=ORCH)
    assert r2.status_code == 200
    assert r2.json() == {**r1.json(), "created": False}
    # A replay writes nothing, not even an audit event.
    assert await read(app, "MATCH (n) RETURN count(n) AS c") == nodes_before

    r3 = await client.post(
        "/v1/investigations", json=create_body("inv_one", objective="Other"), headers=ORCH
    )
    _problem(r3, 409, "content_conflict")
    assert await read(app, "MATCH (n) RETURN count(n) AS c") == nodes_before


async def test_graph_contents_and_audit(client, app: FastAPI) -> None:
    body = create_body("inv_g")
    manifest = load_manifest(MANIFEST)
    r = await client.post(
        "/v1/investigations", json=body, headers={**ORCH, "X-Nexus-Actor": "operator:demo"}
    )
    assert r.status_code == 201

    [inv] = await read(app, "MATCH (i:Investigation {id:'inv_g'}) RETURN i AS i")
    props = dict(inv["i"].properties) if hasattr(inv["i"], "properties") else dict(inv["i"])
    assert props["content_hash"] == create_body_hash("inv_g")
    assert props["scenario_id"] == manifest.scenario_id
    assert props["scenario_version"] == manifest.version
    assert props["grid_pitch"] == manifest.grid.pitch
    assert props["status"] == "CREATED"
    assert props["write_state"] == "OPEN"
    assert props.get("closed_at") is None

    [counts] = await read(
        app,
        "MATCH (i:Investigation {id:'inv_g'}) RETURN "
        "COUNT { (i)-[:HAS_ENTITY]->(:Entity) } AS ents, "
        "COUNT { (i)-[:HAS_EVIDENCE]->(:Evidence) } AS evs, "
        "COUNT { (i)-[:HAS_ENTITY]->(:Location) } AS locs, "
        "COUNT { (i)-[:HAS_ENTITY]->(:Entity)-[:RELATED_TO {type:'USED_BY'}]->(:Entity) } AS used",
    )
    assert counts["ents"] == len(manifest.entities)
    assert counts["evs"] == len(manifest.evidence)
    assert counts["locs"] == sum(1 for e in manifest.entities if e.type.value == "LOCATION")
    assert counts["used"] == sum(1 for e in manifest.entities if e.used_by)
    assert counts["used"] > 0

    [ent] = await read(
        app,
        "MATCH (e:Entity {uid:'inv_g:HDL-ALPHA-1'}) RETURN e.manifest_id AS m, "
        "e.investigation_id AS i, e.type AS t",
    )
    assert ent == {"m": "HDL-ALPHA-1", "i": "inv_g", "t": "HANDLE"}
    locs = await read(app, "MATCH (l:Location {investigation_id:'inv_g'}) RETURN l.position AS p")
    assert all(row["p"].srid == 7203 for row in locs)  # cartesian, not WGS-84
    evs = await read(app, "MATCH (e:Evidence {investigation_id:'inv_g'}) RETURN e.uid AS u")
    assert all(row["u"].startswith("inv_g:") for row in evs)

    audits = await read(
        app,
        "MATCH (a:AuditEvent)-[:CONCERNS]->(:Investigation {id:'inv_g'}) "
        "RETURN a.caller AS caller, a.actor AS actor, a.operation AS op, a.outcome AS outcome, "
        "a.at AS at",
    )
    assert len(audits) == 1
    assert audits[0]["caller"] == "orchestrator"
    assert audits[0]["actor"] == "operator:demo"
    assert audits[0]["op"] == "investigations.create"
    assert audits[0]["outcome"] == "SUCCESS"
    assert audits[0]["at"] is not None


def create_body_hash(inv: str) -> str:
    from nexus_contracts.api.context import ContextInvestigationCreate

    return ContextInvestigationCreate.model_validate(create_body(inv)).content_hash()


async def test_two_investigations_same_manifest_coexist(client, app: FastAPI) -> None:
    manifest = load_manifest(MANIFEST)
    for inv in ("inv_a", "inv_b"):
        r = await client.post("/v1/investigations", json=create_body(inv), headers=ORCH)
        assert r.status_code == 201 and r.json()["created"] is True
    rows = await read(
        app, "MATCH (e:Entity {manifest_id:'ENT-ALPHA'}) RETURN e.investigation_id AS i"
    )
    assert sorted(r["i"] for r in rows) == ["inv_a", "inv_b"]
    [c] = await read(app, "MATCH (e:Entity) RETURN count(e) AS c")
    assert c["c"] == 2 * len(manifest.entities)


async def test_close_idempotent_then_create_rejected(client) -> None:
    body = create_body("inv_c")
    assert (await client.post("/v1/investigations", json=body, headers=ORCH)).status_code == 201
    c1 = await client.post(
        "/v1/investigations/inv_c/close", json={"status": "CANCELLED"}, headers=ORCH
    )
    assert c1.status_code == 200, c1.text
    assert c1.json()["status"] == "CANCELLED"
    c2 = await client.post(
        "/v1/investigations/inv_c/close", json={"status": "COMPLETED"}, headers=ORCH
    )
    assert c2.status_code == 200
    assert c2.json() == c1.json()  # original status and closed_at

    r = await client.post("/v1/investigations", json=body, headers=ORCH)
    _problem(r, 409, "investigation_cancelled")


async def test_close_unknown_and_non_terminal(client) -> None:
    r = await client.post(
        "/v1/investigations/inv_nope/close", json={"status": "FAILED"}, headers=ORCH
    )
    _problem(r, 404)
    r = await client.post(
        "/v1/investigations/inv_nope/close", json={"status": "RUNNING"}, headers=ORCH
    )
    _problem(r, 422, "schema_invalid")


async def test_validation_error_is_problem_details(client) -> None:
    body = create_body("inv_v")
    del body["objective"]
    body["grid_pitch"] = -1
    r = await client.post("/v1/investigations", json=body, headers=ORCH)
    problem = _problem(r, 422, "schema_invalid")
    locs = {tuple(e["loc"]) for e in problem["errors"]}
    assert ("body", "objective") in locs
    assert ("body", "grid_pitch") in locs
    assert all(set(e) == {"loc", "msg", "type"} for e in problem["errors"])


async def test_semantic_validation_rejects_dangling_relationship(client, app) -> None:
    body = create_body("inv_s")
    body["relationships"].append({"type": "USED_BY", "source": "NOPE", "target": "ENT-ALPHA"})
    r = await client.post("/v1/investigations", json=body, headers=ORCH)
    _problem(r, 422, "schema_invalid")
    assert await read(app, "MATCH (i:Investigation {id:'inv_s'}) RETURN i") == []


async def test_concurrent_creates(client, app: FastAPI) -> None:
    ids = [f"inv_cc{i}" for i in range(20)]
    resps = await asyncio.gather(
        *(client.post("/v1/investigations", json=create_body(i), headers=ORCH) for i in ids)
    )
    assert [r.status_code for r in resps] == [201] * 20
    [c] = await read(app, "MATCH (i:Investigation) RETURN count(i) AS c")
    assert c["c"] == 20
    [a] = await read(app, "MATCH (a:AuditEvent) RETURN count(a) AS c")
    assert a["c"] == 20


async def test_concurrent_duplicate_creates_one_wins(client) -> None:
    body = create_body("inv_dup")
    resps = await asyncio.gather(
        *(client.post("/v1/investigations", json=body, headers=ORCH) for _ in range(10))
    )
    assert sorted(r.status_code for r in resps) == [200] * 9 + [201]
