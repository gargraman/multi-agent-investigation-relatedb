"""Clearance-filtered ``GET /context``, read audit and retrieval receipts (S4a FR-201..206).

Real embedded Relate store per test; the analysis-agent token is injected into the registry
(it is not in settings OPTIONAL_CALLERS until S4b)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nexus_context_service.main import create_app
from nexus_contracts.api.context import InvestigationContext
from nexus_contracts.api.problem import ProblemDetails
from nexus_shared.security import TokenRegistry
from nexus_shared.testing import InMemorySpanExporter

from .conftest import (
    GEOINT,
    GEOINT_TOKEN,
    ORCH,
    ORCH_TOKEN,
    SIGINT,
    SIGINT_TOKEN,
    batch_body,
    client_for,
    create_body,
    finding,
    make_settings,
    read,
)
from .test_findings_batch import _geoint_finding

INV = "inv_scope"
ANALYSIS_TOKEN = "a" * 40
TASK = "tsk_read_one"
ANALYSIS = {"Authorization": f"Bearer {ANALYSIS_TOKEN}", "X-Nexus-Task-Id": TASK}
CONTEXT = f"/v1/investigations/{INV}/context"


@dataclass
class Seeded:
    high: str  # DEMO-HIGH, ACTIVE (above analysis clearance)
    untrusted_high: str  # DEMO-HIGH, then NEEDS_REVIEW (must not even be counted for analysis)
    mid: list[str]  # two DEMO-MID GEOINT findings, ACTIVE


@pytest.fixture
async def app(tmp_path: Path) -> FastAPI:
    registry = TokenRegistry(
        {
            "orchestrator": ORCH_TOKEN,
            "sigint-agent": SIGINT_TOKEN,
            "geoint-agent": GEOINT_TOKEN,
            "analysis-agent": ANALYSIS_TOKEN,
        }
    )
    return create_app(
        make_settings(tmp_path), token_registry=registry, span_exporter=InMemorySpanExporter()
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app), client_for(app) as c:
        yield c


@pytest.fixture
async def seeded(client: httpx.AsyncClient, app: FastAPI) -> Seeded:
    r = await client.post("/v1/investigations", json=create_body(INV), headers=ORCH)
    assert r.status_code == 201, r.text
    sig = batch_body(
        INV,
        task_id="tsk_sig",
        findings=[
            finding(classification="DEMO-HIGH"),
            finding(
                "HDL-BRAVO-1", "HDL-DELTA-1", ("ev-sig-003",), day=4, classification="DEMO-HIGH"
            ),
        ],
    )
    r = await client.post("/v1/findings:batch", json=sig, headers=SIGINT)
    assert r.status_code == 201, r.text
    high, untrusted = r.json()["finding_ids"]
    geo = batch_body(
        INV,
        task_id="tsk_geo",
        findings=[_geoint_finding(cell="LOC-GRID-00"), _geoint_finding("ENT-ALPHA", "LOC-GRID-00")],
    )
    r = await client.post("/v1/findings:batch", json=geo, headers=GEOINT)
    assert r.status_code == 201, r.text
    mid = r.json()["finding_ids"]
    await app.state.lane.run(
        "test.retract",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.status = 'NEEDS_REVIEW'", {"id": untrusted}
        ),
    )
    return Seeded(high=high, untrusted_high=untrusted, mid=mid)


async def _audit_rows(app: FastAPI) -> list[dict[str, Any]]:
    return await read(
        app,
        "MATCH (a:AuditEvent {operation:'investigations.context'}) "
        "RETURN a.id AS id, a.caller AS caller, a.outcome AS outcome, a.authz AS authz, "
        "a.filtered_above_clearance AS filtered, a.task_id AS task_id, a.target_id AS target "
        "ORDER BY a.at",
    )


async def test_analysis_context_hides_high_and_counts_it(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.get(CONTEXT, headers=ANALYSIS)
    assert r.status_code == 200, r.text
    ctx = InvestigationContext.model_validate(r.json())
    assert sorted(f.id for f in ctx.findings) == sorted(seeded.mid)
    [audit] = await _audit_rows(app)
    assert audit["caller"] == "analysis-agent"
    assert audit["authz"] == "ALLOWED"
    assert audit["outcome"] == "FILTERED"
    assert audit["filtered"] == 1
    assert audit["task_id"] == TASK
    assert audit["target"] == INV


async def test_analysis_context_body_never_mentions_hidden_id(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.get(CONTEXT, headers=ANALYSIS)
    assert r.status_code == 200
    for hidden in (seeded.high, seeded.untrusted_high):
        assert hidden not in r.text
    # properties(a) yields a plain dict, so the blob really contains every stored property value
    # (a bare node row would repr as an opaque Node object and make this assertion vacuous).
    rows = await read(
        app, "MATCH (a:AuditEvent {operation:'investigations.context'}) RETURN properties(a) AS p"
    )
    assert rows and "filtered_above_clearance" in rows[0]["p"]
    blob = repr(rows)
    assert seeded.high not in blob
    assert seeded.untrusted_high not in blob
    # Nor does any edge of the audit event reach a hidden finding.
    edges = await read(
        app,
        "MATCH (a:AuditEvent {operation:'investigations.context'})-[r]->(f:Finding) "
        "RETURN f.id AS id, type(r) AS t",
    )
    assert {e["id"] for e in edges} == set(seeded.mid)


async def test_receipt_edges_match_response(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.get(CONTEXT, headers=ANALYSIS)
    returned = {f["id"] for f in r.json()["findings"]}
    edges = await read(
        app,
        "MATCH (a:AuditEvent {operation:'investigations.context'})-[:RETURNED]->(f:Finding) "
        "RETURN f.id AS id",
    )
    assert {e["id"] for e in edges} == returned == set(seeded.mid)
    assert len(edges) == 2


async def test_untrusted_not_returned_not_counted(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    # The NEEDS_REVIEW finding is DEMO-HIGH: if it were counted, filtered would be 2.
    r = await client.get(CONTEXT, headers=ANALYSIS)
    assert seeded.untrusted_high not in {f["id"] for f in r.json()["findings"]}
    [audit] = await _audit_rows(app)
    assert audit["filtered"] == 1


async def test_analysis_with_nothing_hidden_is_success(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await app.state.lane.run(
        "test.retract",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.status = 'NEEDS_REVIEW'", {"id": seeded.high}
        ),
    )
    r = await client.get(CONTEXT, headers=ANALYSIS)
    assert r.status_code == 200
    [audit] = await _audit_rows(app)
    assert audit["outcome"] == "SUCCESS"
    assert audit["filtered"] == 0


@pytest.mark.parametrize("task_header", [None, "bogus", "tsk_", "inv_abc"])
async def test_analysis_read_requires_task_header(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded, task_header: str | None
) -> None:
    headers = {"Authorization": f"Bearer {ANALYSIS_TOKEN}"}
    if task_header is not None:
        headers["X-Nexus-Task-Id"] = task_header
    r = await client.get(CONTEXT, headers=headers)
    assert r.status_code == 400, r.text
    problem = ProblemDetails.model_validate(r.json())
    assert problem.code == "schema_invalid"
    assert await _audit_rows(app) == []


async def test_orchestrator_sees_everything_and_header_ignored(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.get(CONTEXT, headers={**ORCH, "X-Nexus-Task-Id": TASK})
    assert r.status_code == 200, r.text
    ids = {f["id"] for f in r.json()["findings"]}
    assert ids == {seeded.high, seeded.untrusted_high, *seeded.mid}
    assert len(ids) == 4
    [audit] = await _audit_rows(app)
    assert audit["caller"] == "orchestrator"
    assert audit["outcome"] == "SUCCESS"
    assert audit["filtered"] == 0
    assert audit["task_id"] is None
    edges = await read(app, "MATCH (:AuditEvent)-[r:RETURNED]->() RETURN count(r) AS c")
    assert edges[0]["c"] == 0


async def test_orchestrator_without_header_still_fine(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    r = await client.get(CONTEXT, headers=ORCH)
    assert r.status_code == 200
    assert len(r.json()["findings"]) == 4


async def test_analysis_edges_of_hidden_findings_absent(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    r = await client.get(CONTEXT, headers=ANALYSIS)
    ctx = InvestigationContext.model_validate(r.json())
    assert all(f.classification.rank <= 2 for f in ctx.findings)
    # The HIGH findings' LINKED_TO assertions are SIGINT contacts; none may surface.
    assert all(a.type != "LINKED_TO" for f in ctx.findings for a in f.assertions)


async def test_unknown_investigation_is_404_and_writes_no_audit(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.get("/v1/investigations/inv_nope/context", headers=ANALYSIS)
    assert r.status_code == 404
    assert await _audit_rows(app) == []


async def test_context_assessment_is_null_without_one(
    client: httpx.AsyncClient, seeded: Seeded
) -> None:
    for headers in (ANALYSIS, ORCH):
        r = await client.get(CONTEXT, headers=headers)
        assert r.json()["assessment"] is None
