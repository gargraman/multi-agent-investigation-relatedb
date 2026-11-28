"""``POST /v1/search`` structured, clearance-filtered search (S4a FR-208, FR-204/205, AC18-a).

Real embedded Relate store per test. The analysis-agent token is injected into the registry (it is
not in settings OPTIONAL_CALLERS until S4b)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nexus_context_service.main import create_app
from nexus_contracts.api.context import InvestigationContext, SearchResponse
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
from .test_read_scope import ANALYSIS, ANALYSIS_TOKEN, TASK

INV = "inv_search"
SEARCH = "/v1/search"


@dataclass
class Seeded:
    high: str  # DEMO-HIGH OBSERVATION about HDL-ALPHA-1 / HDL-CHARLIE-1, ACTIVE
    untrusted_high: str  # DEMO-HIGH OBSERVATION about HDL-BRAVO-1 / HDL-DELTA-1, NEEDS_REVIEW
    mid_obs: str  # DEMO-MID OBSERVATION about ENT-CHARLIE / LOC-GRID-09, ACTIVE
    mid_inf: str  # DEMO-MID INFERENCE about ENT-ALPHA / LOC-GRID-00, ACTIVE

    @property
    def mid(self) -> set[str]:
        return {self.mid_obs, self.mid_inf}


def _make_app(tmp_path: Path, policy: Path | None = None) -> FastAPI:
    registry = TokenRegistry(
        {
            "orchestrator": ORCH_TOKEN,
            "sigint-agent": SIGINT_TOKEN,
            "geoint-agent": GEOINT_TOKEN,
            "analysis-agent": ANALYSIS_TOKEN,
        }
    )
    return create_app(
        make_settings(tmp_path, policy_path=policy),
        token_registry=registry,
        span_exporter=InMemorySpanExporter(),
    )


@pytest.fixture
async def app(tmp_path: Path) -> FastAPI:
    return _make_app(tmp_path)


@pytest.fixture
async def wide_app(tmp_path: Path) -> FastAPI:
    """Shipped policy plus ``search`` for the orchestrator, so the unfiltered (DEMO-HIGH, all
    statuses) path of the query is reachable; the shipped policy lets only analysis search."""
    text = (resources.files("nexus_context_service") / "config/policy.yaml").read_text()
    assert "investigations.create," in text
    wide = tmp_path / "policy-wide.yaml"
    wide.write_text(text.replace("investigations.create,", "investigations.create, search,", 1))
    return _make_app(tmp_path, wide)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app), client_for(app) as c:
        yield c


@pytest.fixture
async def wide_client(wide_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with wide_app.router.lifespan_context(wide_app), client_for(wide_app) as c:
        yield c


async def _seed_investigation(client: httpx.AsyncClient, inv: str) -> None:
    r = await client.post("/v1/investigations", json=create_body(inv), headers=ORCH)
    assert r.status_code == 201, r.text


async def _seed(client: httpx.AsyncClient, app: FastAPI) -> Seeded:
    await _seed_investigation(client, INV)
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
    inference = _geoint_finding("ENT-ALPHA", "LOC-GRID-00")
    inference["kind"] = "INFERENCE"
    geo = batch_body(INV, task_id="tsk_geo", findings=[_geoint_finding(), inference])
    r = await client.post("/v1/findings:batch", json=geo, headers=GEOINT)
    assert r.status_code == 201, r.text
    mid_obs, mid_inf = r.json()["finding_ids"]
    await app.state.lane.run(
        "test.retract",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.status = 'NEEDS_REVIEW'", {"id": untrusted}
        ),
    )
    return Seeded(high=high, untrusted_high=untrusted, mid_obs=mid_obs, mid_inf=mid_inf)


@pytest.fixture
async def seeded(client: httpx.AsyncClient, app: FastAPI) -> Seeded:
    return await _seed(client, app)


@pytest.fixture
async def wide_seeded(wide_client: httpx.AsyncClient, wide_app: FastAPI) -> Seeded:
    return await _seed(wide_client, wide_app)


def _body(**kw: Any) -> dict[str, Any]:
    return {"investigation_id": INV, **kw}


async def _search(
    client: httpx.AsyncClient, headers: dict[str, str] = ANALYSIS, **kw: Any
) -> httpx.Response:
    return await client.post(SEARCH, json=_body(**kw), headers=headers)


def _ids(r: httpx.Response) -> set[str]:
    assert r.status_code == 200, r.text
    return {f["id"] for f in r.json()["findings"]}


async def _audit_rows(app: FastAPI) -> list[dict[str, Any]]:
    return await read(
        app,
        "MATCH (a:AuditEvent {operation:'search'}) "
        "RETURN a.id AS id, a.caller AS caller, a.outcome AS outcome, a.authz AS authz, "
        "a.filtered_above_clearance AS filtered, a.task_id AS task_id, a.target_id AS target "
        "ORDER BY a.at",
    )


# --- filters ---------------------------------------------------------------------------------


async def test_search_filters_and_or(wide_client: httpx.AsyncClient, wide_seeded: Seeded) -> None:
    client, seeded = wide_client, wide_seeded
    everything = {seeded.high, seeded.untrusted_high, *seeded.mid}
    assert _ids(await _search(client, ORCH)) == everything  # no filter

    # entity_ids match through ABOUT; the list is an OR.
    assert _ids(await _search(client, ORCH, entity_ids=["ENT-ALPHA"])) == {seeded.mid_inf}
    assert _ids(await _search(client, ORCH, entity_ids=["LOC-GRID-09"])) == {seeded.mid_obs}
    assert _ids(await _search(client, ORCH, entity_ids=["ENT-ALPHA", "LOC-GRID-09"])) == seeded.mid
    assert _ids(await _search(client, ORCH, entity_ids=["HDL-ALPHA-1"])) == {seeded.high}
    assert _ids(await _search(client, ORCH, entity_ids=["ENT-NOBODY"])) == set()

    # kinds and statuses are ORs inside, ANDs between lists.
    assert _ids(await _search(client, ORCH, kinds=["INFERENCE"])) == {seeded.mid_inf}
    assert _ids(await _search(client, ORCH, kinds=["INFERENCE", "OBSERVATION"])) == everything
    assert _ids(await _search(client, ORCH, statuses=["NEEDS_REVIEW"])) == {seeded.untrusted_high}
    assert _ids(await _search(client, ORCH, statuses=["NEEDS_REVIEW", "ACTIVE"])) == everything

    # Combined filters are AND.
    assert (
        _ids(await _search(client, ORCH, entity_ids=["ENT-ALPHA"], kinds=["OBSERVATION"])) == set()
    )
    assert _ids(await _search(client, ORCH, entity_ids=["ENT-ALPHA"], kinds=["INFERENCE"])) == {
        seeded.mid_inf
    }
    assert (
        _ids(
            await _search(
                client, entity_ids=["HDL-BRAVO-1"], kinds=["OBSERVATION"], statuses=["ACTIVE"]
            )
        )
        == set()
    )  # right entity and kind, but that finding is NEEDS_REVIEW


async def test_search_rows_equal_context_rows(
    wide_client: httpx.AsyncClient, wide_seeded: Seeded
) -> None:
    """Search assembles findings exactly as /context does (one shared assembly helper)."""
    client = wide_client
    ctx = InvestigationContext.model_validate(
        (await client.get(f"/v1/investigations/{INV}/context", headers=ORCH)).json()
    )
    r = await _search(client, ORCH)
    found = SearchResponse.model_validate(r.json())
    assert found.findings == ctx.findings
    assert found.truncated is False
    assert any(f.assertions and f.evidence and f.about for f in found.findings)


async def test_search_orders_by_created_at_then_id(
    wide_client: httpx.AsyncClient, wide_seeded: Seeded
) -> None:
    r = await _search(wide_client, ORCH)
    findings = SearchResponse.model_validate(r.json()).findings
    keys = [(f.created_at, f.id) for f in findings]
    assert keys == sorted(keys)


# --- clearance, audit, receipts --------------------------------------------------------------


async def test_search_hides_high_and_audits(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await _search(client, ANALYSIS)
    assert _ids(r) == seeded.mid
    for hidden in (seeded.high, seeded.untrusted_high):
        assert hidden not in r.text
    [audit] = await _audit_rows(app)
    assert audit["caller"] == "analysis-agent"
    assert audit["authz"] == "ALLOWED"
    assert audit["outcome"] == "FILTERED"
    assert audit["filtered"] == 1  # the ACTIVE DEMO-HIGH; the NEEDS_REVIEW one is not counted
    assert audit["task_id"] == TASK
    assert audit["target"] == INV
    # Receipt edges: exactly the returned ids, and the audit properties carry no hidden id.
    edges = await read(
        app,
        "MATCH (:AuditEvent {operation:'search'})-[:RETURNED]->(f:Finding) RETURN f.id AS id",
    )
    assert {e["id"] for e in edges} == seeded.mid
    [props] = await read(app, "MATCH (a:AuditEvent {operation:'search'}) RETURN properties(a) AS p")
    assert seeded.high not in repr(props)
    assert seeded.untrusted_high not in repr(props)


async def test_search_filtered_count_respects_other_filters(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    # The only above-clearance finding is an OBSERVATION: filtering to INFERENCE hides nothing.
    r = await _search(client, ANALYSIS, kinds=["INFERENCE"])
    assert _ids(r) == {seeded.mid_inf}
    await _search(client, ANALYSIS, kinds=["OBSERVATION"])
    await _search(client, ANALYSIS, entity_ids=["HDL-ALPHA-1"])
    await _search(client, ANALYSIS, entity_ids=["ENT-ALPHA"])
    rows = await _audit_rows(app)
    assert [(a["outcome"], a["filtered"]) for a in rows] == [
        ("SUCCESS", 0),
        ("FILTERED", 1),
        ("FILTERED", 1),
        ("SUCCESS", 0),
    ]


async def test_search_analysis_cannot_ask_for_untrusted(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await _search(client, ANALYSIS, statuses=["NEEDS_REVIEW"])
    assert r.status_code == 200
    assert r.json() == {"findings": [], "truncated": False}
    # Untrusted findings are never counted as filtered, so nothing was hidden by clearance here.
    [audit] = await _audit_rows(app)
    assert audit["filtered"] == 0
    assert audit["outcome"] == "SUCCESS"
    # Mixed request: the trusted half still comes back, the untrusted half does not.
    r = await _search(client, ANALYSIS, statuses=["NEEDS_REVIEW", "ACTIVE"])
    assert _ids(r) == seeded.mid


async def test_search_orchestrator_sees_all_and_header_ignored(
    wide_client: httpx.AsyncClient, wide_app: FastAPI, wide_seeded: Seeded
) -> None:
    client, app = wide_client, wide_app
    r = await _search(client, {**ORCH, "X-Nexus-Task-Id": TASK})
    assert len(_ids(r)) == 4
    [audit] = await _audit_rows(app)
    assert audit["caller"] == "orchestrator"
    assert audit["outcome"] == "SUCCESS"
    assert audit["filtered"] == 0
    assert audit["task_id"] is None
    [row] = await read(app, "MATCH (:AuditEvent)-[r:RETURNED]->() RETURN count(r) AS c")
    assert row["c"] == 0


@pytest.mark.parametrize("task_header", [None, "bogus", "tsk_", "inv_abc"])
async def test_search_analysis_requires_task_header(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded, task_header: str | None
) -> None:
    headers = {"Authorization": f"Bearer {ANALYSIS_TOKEN}"}
    if task_header is not None:
        headers["X-Nexus-Task-Id"] = task_header
    r = await _search(client, headers)
    assert r.status_code == 400, r.text
    assert ProblemDetails.model_validate(r.json()).code == "schema_invalid"
    assert await _audit_rows(app) == []


@pytest.mark.parametrize("headers", [SIGINT, GEOINT, ORCH])
async def test_search_only_for_analysis_under_shipped_policy(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded, headers: dict[str, str]
) -> None:
    r = await _search(client, headers)
    assert r.status_code == 403
    [denied] = await read(
        app, "MATCH (a:AuditEvent {operation:'search', authz:'DENIED'}) RETURN a.outcome AS o"
    )
    assert denied["o"] == "DENIED"
    assert seeded.high not in r.text


# --- limit, errors ---------------------------------------------------------------------------


async def test_search_truncates(wide_client: httpx.AsyncClient, wide_seeded: Seeded) -> None:
    client = wide_client
    # Three ACTIVE findings match; limit 2 returns two and says there are more.
    r = await _search(client, ORCH, statuses=["ACTIVE"], limit=2)
    assert r.status_code == 200
    resp = SearchResponse.model_validate(r.json())
    assert len(resp.findings) == 2 and resp.truncated is True
    r = await _search(client, ORCH, statuses=["ACTIVE"], limit=3)
    resp = SearchResponse.model_validate(r.json())
    assert len(resp.findings) == 3 and resp.truncated is False


async def test_search_truncated_receipts_cover_returned_only(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await _search(client, ANALYSIS, limit=1)
    returned = _ids(r)
    assert len(returned) == 1 and r.json()["truncated"] is True
    edges = await read(app, "MATCH (:AuditEvent)-[:RETURNED]->(f:Finding) RETURN f.id AS id")
    assert {e["id"] for e in edges} == returned


@pytest.mark.parametrize("limit", [0, 501, -1])
async def test_search_limit_bounds(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded, limit: int
) -> None:
    r = await _search(client, limit=limit)
    assert r.status_code == 422
    assert await _audit_rows(app) == []


async def test_search_unknown_investigation_404(client: httpx.AsyncClient, app: FastAPI) -> None:
    r = await client.post(SEARCH, json={"investigation_id": "inv_nope"}, headers=ANALYSIS)
    assert r.status_code == 404
    assert await _audit_rows(app) == []


async def test_search_requires_auth(client: httpx.AsyncClient, seeded: Seeded) -> None:
    r = await client.post(SEARCH, json=_body())
    assert r.status_code == 401
