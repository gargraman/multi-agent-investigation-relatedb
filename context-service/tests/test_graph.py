"""``GET /v1/investigations/{id}/graph``: I6 knowledge-graph view (S4a FR-204, FR-219, NFR-202).

Reuses the Task 5 fixtures (analysis token injected into the registry, two seeded investigations,
retrieve + submit helpers). Clearance-filtering at a lower rank is exercised at repository level
by building a ``ReadScope`` (G7: the HTTP surface has no "view at clearance" parameter)."""

import json
from typing import Any

import httpx
from fastapi import FastAPI

from nexus_context_service.repository import AuditContext
from nexus_context_service.scope import ReadScope
from nexus_contracts.api.graph import Graph, GraphNodeLabel

from . import test_assessments as base
from .conftest import ORCH, read
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

OPERATOR = {**ORCH, "X-Nexus-Actor": "operator:demo"}
GRAPH = f"/v1/investigations/{INV}/graph"

FINDING_PROPS = {
    "kind",
    "status",
    "statement",
    "confidence_band",
    "classification",
    "source_agent_id",
}
WHITELIST = {
    "Investigation": {"status", "scenario_id"},
    "Entity": {"manifest_id", "type", "status"},
    "Evidence": {"manifest_id", "classification", "status"},
    "Finding": FINDING_PROPS,
    "Assertion": {"type", "valid_from", "valid_until", "link_type"},
    "Assessment": {"status", "summary", "classification"},
    "Decision": {"actor", "created_at"},
}


async def _assess(client: httpx.AsyncClient, seeded: Seeded) -> str:
    """Retrieve for the task (writes the receipt), then commit an assessment over f1 and f2."""
    await retrieve(client)
    body = assessment_body([seeded.f1, seeded.f2])
    return (await _post_ok(client, URL, body, ANALYSIS_POST))["assessment_id"]


async def _graph_audits(app: FastAPI) -> list[dict[str, Any]]:
    return await read(
        app,
        "MATCH (a:AuditEvent {operation:'investigations.graph'}) "
        "RETURN a.caller AS caller, a.outcome AS outcome, a.authz AS authz, "
        "a.filtered_above_clearance AS filtered, a.target_id AS target, "
        "properties(a) AS props ORDER BY a.at",
    )


async def test_graph_shape(client: httpx.AsyncClient, app: FastAPI, seeded: Seeded) -> None:
    asm = await _assess(client, seeded)
    r = await client.post(
        f"/v1/investigations/{INV}/evidence/ev-sig-003/retract",
        json={"reason": "tampered"},
        headers=OPERATOR,
    )
    assert r.status_code == 201, r.text

    r = await client.get(GRAPH, headers=ORCH)
    assert r.status_code == 200, r.text
    graph = Graph.model_validate(r.json())

    labels = {n.label for n in graph.nodes}
    assert labels == set(GraphNodeLabel)  # all seven are present on this seed
    ids = {n.id for n in graph.nodes}
    assert len(ids) == len(graph.nodes)  # ids are unique across labels
    assert not any(i.startswith(("aud_", "sub_")) for i in ids)
    for e in graph.edges:
        assert e.source in ids and e.target in ids, e
    assert len({(e.source, e.target, e.type) for e in graph.edges}) == len(graph.edges)

    for n in graph.nodes:
        assert set(n.props) == WHITELIST[n.label.value], n
    by_label: dict[str, list[Any]] = {}
    for n in graph.nodes:
        by_label.setdefault(n.label.value, []).append(n)
    assert {n.id for n in by_label["Finding"]} == {
        seeded.high,
        seeded.low,
        seeded.f1,
        seeded.f2,
    }
    for n in by_label["Finding"]:
        assert set(n.props) == FINDING_PROPS
    assert {n.id for n in by_label["Assessment"]} == {asm}
    assert "body_json" not in r.text and "conclusions" not in r.text
    [decision] = by_label["Decision"]
    assert decision.props["actor"] == "operator:demo"
    assert isinstance(decision.props["created_at"], str)
    assert {f"{INV}:{e}" for e in ("ev-sig-001", "ev-sig-002", "ev-sig-003", "ev-geo-001")} <= {
        n.id for n in by_label["Evidence"]
    }
    # assertion datetimes are serialised as strings (props are scalar-only)
    assertions = by_label["Assertion"]
    assert assertions and all(isinstance(a.props["valid_from"], str) for a in assertions)

    types = {e.type for e in graph.edges}
    assert {
        "HAS_ENTITY",
        "HAS_EVIDENCE",
        "CONTAINS",
        "BELONGS_TO",
        "ABOUT",
        "SUPPORTED_BY",
        "ASSERTS",
        "SUBJECT",
        "OBJECT",
        "AT",
        "USES_FINDING",
        "DISPOSES",
        "RELATED_TO",
    } <= types
    assert not types & {"CONCERNS", "COMMITTED", "RETURNED"}
    assert (decision.id, f"{INV}:ev-sig-003", "DISPOSES") in {
        (e.source, e.target, e.type) for e in graph.edges
    }
    assert {(e.source, e.target) for e in graph.edges if e.type == "USES_FINDING"} == {
        (asm, seeded.f1),
        (asm, seeded.f2),
    }


async def test_graph_filters_at_lower_rank(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    asm = await _assess(client, seeded)
    # An assessment above clearance (an analysis write can't produce one, so set it directly).
    await app.state.lane.run(
        "test.raise",
        lambda conn: conn.execute(
            "MATCH (a:Assessment {id:$id}) "
            "SET a.classification = 'DEMO-HIGH', a.classification_rank = 3",
            {"id": asm},
        ),
    )
    assertions_of_high = [
        r["id"]
        for r in await read(
            app,
            "MATCH (:Finding {id:$id})-[:ASSERTS]->(a:Assertion) RETURN a.id AS id",
            {"id": seeded.high},
        )
    ]
    assert assertions_of_high

    # finding -> finding edges: one to the hidden finding, one between visible findings
    await app.state.lane.run(
        "test.edges",
        lambda conn: [
            conn.execute(
                f"MATCH (s:Finding {{id:$s}}), (t:Finding {{id:$t}}) CREATE (s)-[:{rel}]->(t)",
                {"s": s, "t": t},
            )
            for rel, s, t in (
                ("CORROBORATES", seeded.f1, seeded.high),
                ("CORROBORATES", seeded.f1, seeded.f2),
            )
        ],
    )

    scope = ReadScope(
        audit=AuditContext(caller="orchestrator", actor="orchestrator"),
        max_rank=2,
        trusted_only=False,
        task_id=None,
    )
    graph = await app.state.repository.graph(INV, scope)

    ids = {n.id for n in graph.nodes}
    assert seeded.high not in ids
    assert not ids & set(assertions_of_high)
    assert asm not in ids
    assert {seeded.low, seeded.f1, seeded.f2} <= ids
    raw = graph.model_dump_json()
    for hidden in (seeded.high, asm, *assertions_of_high):
        assert hidden not in raw
    for e in graph.edges:
        assert e.source in ids and e.target in ids
    corroborates = {(e.source, e.target) for e in graph.edges if e.type == "CORROBORATES"}
    assert corroborates == {(seeded.f1, seeded.f2)}  # the edge to the hidden finding is dropped

    [audit] = await _graph_audits(app)
    assert audit["outcome"] == "FILTERED"
    assert audit["filtered"] == 2  # one finding, one assessment
    assert audit["target"] == INV
    assert audit["authz"] == "ALLOWED"
    blob = json.dumps(audit["props"], default=str)
    for hidden in (seeded.high, asm, *assertions_of_high):
        assert hidden not in blob


async def test_graph_orchestrator_audit_success(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.get(GRAPH, headers=ORCH)
    assert r.status_code == 200, r.text
    assert seeded.high in {n["id"] for n in r.json()["nodes"]}  # orchestrator sees DEMO-HIGH
    [audit] = await _graph_audits(app)
    assert (audit["caller"], audit["outcome"], audit["authz"]) == (
        "orchestrator",
        "SUCCESS",
        "ALLOWED",
    )
    assert audit["filtered"] == 0 and audit["target"] == INV
    # the read audit is not itself part of the graph
    assert "aud_" not in r.text


async def test_graph_is_scoped_to_its_investigation(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    rb = await client.post(
        f"/v1/investigations/{INV_B}/evidence/ev-sig-003/retract", headers=OPERATOR
    )
    assert rb.status_code == 201, rb.text
    decision_b = rb.json()["decision_id"]

    a = Graph.model_validate((await client.get(GRAPH, headers=ORCH)).json())
    b = Graph.model_validate(
        (await client.get(f"/v1/investigations/{INV_B}/graph", headers=ORCH)).json()
    )
    a_ids, b_ids = {n.id for n in a.nodes}, {n.id for n in b.nodes}
    assert seeded.foreign not in a_ids and decision_b not in a_ids
    assert not any(i.startswith(f"{INV_B}:") or i == INV_B for i in a_ids)
    assert seeded.foreign in b_ids and decision_b in b_ids
    assert not b_ids & {seeded.high, seeded.low, seeded.f1, seeded.f2}
    assert not any(i.startswith(f"{INV}:") or i == INV for i in b_ids)
    for g, ids in ((a, a_ids), (b, b_ids)):
        assert all(e.source in ids and e.target in ids for e in g.edges)


async def test_graph_unknown_investigation_and_denied_callers(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.get("/v1/investigations/inv_nope/graph", headers=ORCH)
    assert r.status_code == 404
    assert await _graph_audits(app) == []  # the read rolled back with its audit event
    # only the orchestrator is granted investigations.graph
    assert (await client.get(GRAPH, headers=ANALYSIS_POST)).status_code == 403
    assert (await client.get(GRAPH)).status_code == 401
