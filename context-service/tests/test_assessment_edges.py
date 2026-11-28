"""Edge validation on ``POST /v1/assessments`` and ``DISPUTED`` on contradiction
(S4a FR-209, FR-211a, FR-213, AC9-s part 1).

Reuses the Task 5 fixtures and helpers (analysis token injected into the registry, seeded
investigation, retrieval + submit helpers)."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nexus_context_service.assessments import FindingFacts, validate_edge
from nexus_contracts.api.assessments import AssessmentSubmissionResponse, ProposedEdge
from nexus_contracts.common import FindingKind, FindingStatus

from . import test_assessments as base
from .conftest import GEOINT, batch_body, read
from .test_assessments import (
    ANALYSIS_POST,
    INV,
    URL,
    Seeded,
    _post_ok,
    _set_finding_status,
    assessment_body,
    retrieve,
)
from .test_findings_batch import _geoint_finding

# Fixture objects re-exported by assignment (not import-by-name) so pytest finds them here
# without ruff seeing a redefinition.
app = base.app
client = base.client
seeded = base.seeded


def edge(kind: str, src: str, tgt: str) -> dict[str, str]:
    return {"type": kind, "source_finding_id": src, "target_finding_id": tgt}


async def submit(
    client: httpx.AsyncClient,
    supporting: list[str],
    edges: list[dict[str, str]],
    task: str,
    *,
    do_retrieve: bool = True,
) -> AssessmentSubmissionResponse:
    """Retrieve for ``task`` (writes the receipt), then submit an assessment with ``edges``."""
    if do_retrieve:
        await retrieve(client, task=task)
    body = assessment_body(supporting, task=task)
    body["proposed_edges"] = edges
    return AssessmentSubmissionResponse.model_validate(
        await _post_ok(client, URL, body, ANALYSIS_POST)
    )


async def seed_inference(client: httpx.AsyncClient, n: int = 3) -> list[str]:
    """``n`` GEOINT-written ``INFERENCE`` findings (the batch validator allows the kind for an
    agent), DEMO-MID so analysis can retrieve them."""
    people = ["ENT-ALPHA", "ENT-BRAVO", "ENT-CHARLIE"]
    findings = []
    for person in people[:n]:
        f = _geoint_finding(person, "LOC-GRID-05")
        f["kind"] = "INFERENCE"
        findings.append(f)
    r = await client.post(
        "/v1/findings:batch",
        json=batch_body(INV, task_id="tsk_geo_inf", findings=findings),
        headers=GEOINT,
    )
    assert r.status_code == 201, r.text
    return r.json()["finding_ids"]


async def finding_row(app: FastAPI, fid: str) -> dict[str, Any]:
    [row] = await read(
        app,
        "MATCH (f:Finding {id:$id}) RETURN f.status AS status, f.version AS version, "
        "f.statement AS statement, f.classification AS classification, "
        "f.confidence AS confidence",
        {"id": fid},
    )
    return row


async def edge_count(app: FastAPI, rel: str) -> int:
    [row] = await read(app, f"MATCH (:Finding)-[r:{rel}]->(:Finding) RETURN count(r) AS n")
    return row["n"]


# --- pure rule ----------------------------------------------------------------------------------


def test_validate_edge_rules() -> None:
    obs = FindingFacts(kind=FindingKind.OBSERVATION, status=FindingStatus.ACTIVE)
    inf = FindingFacts(kind=FindingKind.INFERENCE, status=FindingStatus.ACTIVE)
    visible = {"fnd_a": obs, "fnd_b": inf}
    receipt = {"fnd_a", "fnd_b", "fnd_c"}  # fnd_c: retrieved but not visible (above clearance)

    def check(kind: str, src: str, tgt: str) -> str | None:
        e = ProposedEdge.model_validate(edge(kind, src, tgt))
        code = validate_edge(e, receipt, visible)
        return code.value if code else None

    assert check("CORROBORATES", "fnd_a", "fnd_b") is None
    assert check("CORROBORATES", "fnd_a", "fnd_z") == "unknown_finding_reference"  # not retrieved
    assert check("CORROBORATES", "fnd_a", "fnd_c") == "unknown_finding_reference"  # not visible
    assert check("CORROBORATES", "fnd_c", "fnd_a") == "unknown_finding_reference"
    assert check("DERIVED_FROM", "fnd_b", "fnd_a") is None
    assert check("DERIVED_FROM", "fnd_a", "fnd_b") == "schema_invalid"  # OBSERVATION source
    # a reference problem wins over a schema problem
    assert check("DERIVED_FROM", "fnd_a", "fnd_z") == "unknown_finding_reference"


# --- AC9-s part 1 -------------------------------------------------------------------------------


async def test_contradicts_disputes_both_and_keeps_them(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    before = {fid: await finding_row(app, fid) for fid in (seeded.f1, seeded.f2)}
    assert all(r["status"] == "ACTIVE" and r["version"] == 1 for r in before.values())
    e = edge("CONTRADICTS", seeded.f1, seeded.f2)
    resp = await submit(client, [seeded.f1, seeded.f2], [e], base.TASK)
    assert [x.model_dump(mode="json") for x in resp.accepted_edges] == [e]
    assert resp.rejected_edges == []
    for fid, old in before.items():
        now = await finding_row(app, fid)
        assert now == {**old, "status": "DISPUTED", "version": 2}  # nothing else changed
    [rel] = await read(
        app,
        "MATCH (a:Finding {id:$a})-[r:CONTRADICTS]->(b:Finding {id:$b}) "
        "RETURN r.assessment_id AS assessment_id, r.created_at AS created_at",
        {"a": seeded.f1, "b": seeded.f2},
    )
    assert rel["assessment_id"] == resp.assessment_id and rel["created_at"]
    # DISPUTED is trusted: both are still returned to analysis.
    assert {seeded.f1, seeded.f2} <= await retrieve(client, task="tsk_analysis_later")
    # The stored response of a replay is the same, and the replay changes nothing.
    r = await client.post(
        URL, json={**_edge_body(seeded, [e]), "attempt": 2}, headers=ANALYSIS_POST
    )
    assert r.status_code == 200, r.text
    assert AssessmentSubmissionResponse.model_validate(r.json()) == resp
    assert (await finding_row(app, seeded.f1))["version"] == 2


def _edge_body(seeded: Seeded, edges: list[dict[str, str]]) -> dict[str, Any]:
    body = assessment_body([seeded.f1, seeded.f2])
    body["proposed_edges"] = edges
    return body


@pytest.mark.parametrize("untouched", ["NEEDS_REVIEW", "SUPERSEDED", "DISPUTED"])
async def test_contradicts_leaves_needs_review_alone(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded, untouched: str
) -> None:
    """Only ACTIVE moves to DISPUTED; the other statuses (and their version) are left alone."""
    await retrieve(client)  # receipt first, while f1 is still trusted
    await _set_finding_status(app, seeded.f1, untouched)
    e = edge("CONTRADICTS", seeded.f1, seeded.f2)
    resp = await submit(client, [seeded.f2], [e], base.TASK, do_retrieve=False)
    assert len(resp.accepted_edges) == 1
    f1 = await finding_row(app, seeded.f1)
    assert (f1["status"], f1["version"]) == (untouched, 1)
    f2 = await finding_row(app, seeded.f2)
    assert (f2["status"], f2["version"]) == ("DISPUTED", 2)


async def test_rejected_contradiction_disputes_nothing(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    resp = await submit(
        client, [seeded.f1], [edge("CONTRADICTS", seeded.f1, seeded.high)], base.TASK
    )
    assert resp.accepted_edges == []
    assert (await finding_row(app, seeded.f1))["status"] == "ACTIVE"
    assert (await finding_row(app, seeded.high))["status"] == "ACTIVE"
    assert await edge_count(app, "CONTRADICTS") == 0


# --- per-edge rejection -------------------------------------------------------------------------


async def test_bad_edge_rejected_individually(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await retrieve(client)
    geo = batch_body(INV, task_id="tsk_geo3", findings=[_geoint_finding("ENT-BRAVO")])
    [f3] = (await _post_ok(client, "/v1/findings:batch", geo, GEOINT))["finding_ids"]
    good = edge("CORROBORATES", seeded.f1, seeded.f2)
    bad = edge("CORROBORATES", seeded.f1, f3)  # f3 exists and is visible, never retrieved
    resp = await submit(client, [seeded.f1], [good, bad], base.TASK, do_retrieve=False)
    assert [e.model_dump(mode="json") for e in resp.accepted_edges] == [good]
    assert [(e.edge.model_dump(mode="json"), e.code) for e in resp.rejected_edges] == [
        (bad, "unknown_finding_reference")
    ]
    assert await edge_count(app, "CORROBORATES") == 1


async def test_hidden_foreign_and_missing_endpoints_report_the_same_code(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await retrieve(client)
    await retrieve(client, base.INV_B)
    targets = [seeded.high, seeded.foreign, "fnd_doesnotexist"]
    edges = [edge("CORROBORATES", seeded.f1, t) for t in targets]
    resp = await submit(client, [seeded.f1], edges, base.TASK, do_retrieve=False)
    assert resp.accepted_edges == []
    assert [e.code for e in resp.rejected_edges] == ["unknown_finding_reference"] * 3
    assert await edge_count(app, "CORROBORATES") == 0


async def test_retrieved_but_reclassified_endpoint_is_rejected(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """Same rank-visibility as the citation check: retrieved earlier, now above clearance."""
    await retrieve(client)
    await app.state.lane.run(
        "test.reclassify",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.classification_rank = 3", {"id": seeded.f2}
        ),
    )
    resp = await submit(
        client,
        [seeded.f1],
        [edge("CORROBORATES", seeded.f1, seeded.f2)],
        base.TASK,
        do_retrieve=False,
    )
    assert [e.code for e in resp.rejected_edges] == ["unknown_finding_reference"]


# --- DERIVED_FROM -------------------------------------------------------------------------------


async def test_derived_from_needs_inference_source(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """The INFERENCE findings are seeded through the findings batch (allowed for an agent)."""
    [inf, *_] = await seed_inference(client)
    bad = edge("DERIVED_FROM", seeded.f1, inf)  # OBSERVATION source
    good = edge("DERIVED_FROM", inf, seeded.f1)
    resp = await submit(client, [inf], [bad, good], base.TASK)
    assert [e.model_dump(mode="json") for e in resp.accepted_edges] == [good]
    assert [(e.edge.model_dump(mode="json"), e.code) for e in resp.rejected_edges] == [
        (bad, "schema_invalid")
    ]
    [rel] = await read(
        app,
        "MATCH (a:Finding {id:$a})-[r:DERIVED_FROM]->(b:Finding {id:$b}) "
        "RETURN r.assessment_id AS assessment_id, r.created_at AS created_at",
        {"a": inf, "b": seeded.f1},
    )
    assert rel["assessment_id"] == resp.assessment_id and rel["created_at"]
    assert await edge_count(app, "DERIVED_FROM") == 1


async def test_derived_from_cycle_rejected(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    i1, i2, i3 = await seed_inference(client)
    chain = [edge("DERIVED_FROM", i1, i2), edge("DERIVED_FROM", i2, i3)]
    # Within one submission: the third edge closes the cycle opened by the first two.
    closing = edge("DERIVED_FROM", i3, i1)
    resp = await submit(client, [i1], [*chain, closing], base.TASK)
    assert [e.model_dump(mode="json") for e in resp.accepted_edges] == chain
    assert [(e.edge.model_dump(mode="json"), e.code) for e in resp.rejected_edges] == [
        (closing, "schema_invalid")
    ]
    # Across submissions: the reverse of a stored edge is a two-cycle.
    reverse = edge("DERIVED_FROM", i2, i1)
    resp = await submit(client, [i2], [reverse], "tsk_analysis_two")
    assert [(e.code) for e in resp.rejected_edges] == ["schema_invalid"]
    assert await edge_count(app, "DERIVED_FROM") == 2


# --- existing edges -----------------------------------------------------------------------------


async def test_existing_edge_not_duplicated(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    first = await submit(
        client, [seeded.f1], [edge("CONTRADICTS", seeded.f1, seeded.f2)], base.TASK
    )
    assert len(first.accepted_edges) == 1
    reverse = edge("CONTRADICTS", seeded.f2, seeded.f1)
    second = await submit(client, [seeded.f2], [reverse], "tsk_analysis_two")
    assert [e.model_dump(mode="json") for e in second.accepted_edges] == [reverse]
    assert second.rejected_edges == []
    assert await edge_count(app, "CONTRADICTS") == 1
    # The already-DISPUTED pair is not bumped a second time.
    assert [(await finding_row(app, f))["version"] for f in (seeded.f1, seeded.f2)] == [2, 2]


async def test_existing_directed_edge_not_duplicated(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    i1, i2, _ = await seed_inference(client)
    e = edge("DERIVED_FROM", i1, i2)
    await submit(client, [i1], [e], base.TASK)
    again = await submit(client, [i1], [e], "tsk_analysis_two")
    assert [x.model_dump(mode="json") for x in again.accepted_edges] == [e]
    assert await edge_count(app, "DERIVED_FROM") == 1
