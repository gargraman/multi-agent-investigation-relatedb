"""``POST /v1/assessments``: write, idempotency, receipt-based citation check, retraction race
(S4a FR-207, FR-209..212, FR-211a, AC7-s, Trust-a, Review Focus 1-2).

Real embedded Relate store per test. The analysis-agent token is injected into the registry (it is
not in settings OPTIONAL_CALLERS until S4b)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nexus_context_service.assessments import assessment_hash, assessment_key, used_finding_ids
from nexus_context_service.main import create_app
from nexus_contracts.api.assessments import AssessmentSubmission, AssessmentSubmissionResponse
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

INV = "inv_asm"
INV_B = "inv_asm_b"
ANALYSIS_TOKEN = "a" * 40
TASK = "tsk_analysis_one"
URL = "/v1/assessments"


def analysis(task: str = TASK) -> dict[str, str]:
    return {"Authorization": f"Bearer {ANALYSIS_TOKEN}", "X-Nexus-Task-Id": task}


ANALYSIS_POST = {"Authorization": f"Bearer {ANALYSIS_TOKEN}"}


@dataclass
class Seeded:
    high: str  # DEMO-HIGH SIGINT finding, ACTIVE: never visible to analysis
    low: str  # DEMO-LOW SIGINT finding, ACTIVE
    f1: str  # DEMO-MID GEOINT finding, ACTIVE
    f2: str  # DEMO-MID GEOINT finding, ACTIVE
    foreign: str  # DEMO-MID GEOINT finding of INV_B


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


async def _post_ok(client: httpx.AsyncClient, url: str, body: Any, headers: dict) -> Any:
    r = await client.post(url, json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
async def seeded(client: httpx.AsyncClient) -> Seeded:
    for inv in (INV, INV_B):
        await _post_ok(client, "/v1/investigations", create_body(inv), ORCH)
    sig = batch_body(
        INV,
        task_id="tsk_sig",
        findings=[
            finding(classification="DEMO-HIGH"),
            finding("HDL-BRAVO-1", "HDL-DELTA-1", ("ev-sig-003",), day=4),
        ],
    )
    high, low = (await _post_ok(client, "/v1/findings:batch", sig, SIGINT))["finding_ids"]
    geo = batch_body(
        INV,
        task_id="tsk_geo",
        findings=[_geoint_finding(cell="LOC-GRID-00"), _geoint_finding("ENT-ALPHA", "LOC-GRID-00")],
    )
    f1, f2 = (await _post_ok(client, "/v1/findings:batch", geo, GEOINT))["finding_ids"]
    geo_b = batch_body(INV_B, task_id="tsk_geo_b", findings=[_geoint_finding()])
    [foreign] = (await _post_ok(client, "/v1/findings:batch", geo_b, GEOINT))["finding_ids"]
    return Seeded(high=high, low=low, f1=f1, f2=f2, foreign=foreign)


async def retrieve(client: httpx.AsyncClient, inv: str = INV, task: str = TASK) -> set[str]:
    """An analysis read: writes the receipt (RETURNED edges) keyed on ``task``."""
    r = await client.get(f"/v1/investigations/{inv}/context", headers=analysis(task))
    assert r.status_code == 200, r.text
    return {f["id"] for f in r.json()["findings"]}


def assessment_body(
    supporting: list[str],
    *,
    contradiction: list[str] | None = None,
    task: str = TASK,
    inv: str = INV,
    attempt: int = 1,
    summary: str = "ALPHA and CHARLIE were co-located.",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "task_id": task,
        "investigation_id": inv,
        "attempt": attempt,
        "schema_version": "1.0.0",
        "summary": summary,
        "conclusions": [
            {
                "statement": "Co-location is supported by GEOINT.",
                "kind": "INFERENCE",
                "confidence": 0.7,
                "confidence_band": "MODERATE",
                "supporting_finding_ids": supporting,
                "limitations": ["Single source."],
            }
        ],
        "contradictions": [],
        "information_gaps": ["No SIGINT on DELTA."],
        "follow_up_questions": ["Was BRAVO present?"],
        "model_id": "replay:gpt-test",
        "prompt_version": "analysis-assess@1.0.0",
        "proposed_edges": [],
    }
    if contradiction:
        body["contradictions"] = [
            {
                "finding_ids": contradiction,
                "description": "Different cells at overlapping times.",
                "effect_on_confidence": "Lowers confidence.",
            }
        ]
    return body


async def _counts(app: FastAPI) -> dict[str, int]:
    rows = await read(
        app,
        "OPTIONAL MATCH (a:Assessment) WITH count(a) AS assessments "
        "OPTIONAL MATCH (s:Submission) WHERE s.idempotency_key STARTS WITH 'assessment:' "
        "WITH assessments, count(s) AS submissions "
        "OPTIONAL MATCH (e:AuditEvent {operation:'assessments.create'}) "
        "RETURN assessments, submissions, count(e) AS audits",
    )
    return rows[0]


# --- pure helpers -------------------------------------------------------------------------------


def test_helpers() -> None:
    body = AssessmentSubmission.model_validate(
        assessment_body(["fnd_b", "fnd_a"], contradiction=["fnd_c", "fnd_a"])
    )
    assert assessment_key("tsk_x") == "assessment:tsk_x"
    assert used_finding_ids(body) == ["fnd_a", "fnd_b", "fnd_c"]
    retry = AssessmentSubmission.model_validate(
        assessment_body(["fnd_b", "fnd_a"], contradiction=["fnd_c", "fnd_a"], attempt=3)
    )
    assert assessment_hash(body) == assessment_hash(retry)
    other = AssessmentSubmission.model_validate(
        assessment_body(["fnd_b", "fnd_a"], contradiction=["fnd_c", "fnd_a"], summary="Other.")
    )
    assert assessment_hash(body) != assessment_hash(other)


# --- write path ---------------------------------------------------------------------------------


async def test_assessment_commits_with_uses_finding(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """AC7-s part 2."""
    assert await retrieve(client) == {seeded.low, seeded.f1, seeded.f2}
    body = assessment_body([seeded.f1, seeded.low], contradiction=[seeded.f1, seeded.f2])
    r = await client.post(URL, json=body, headers=ANALYSIS_POST)
    assert r.status_code == 201, r.text
    resp = AssessmentSubmissionResponse.model_validate(r.json())
    assert resp.assessment_id.startswith("asm_")
    used = await read(
        app,
        "MATCH (a:Assessment {id:$id})-[:USES_FINDING]->(f:Finding) RETURN f.id AS id",
        {"id": resp.assessment_id},
    )
    assert sorted(u["id"] for u in used) == sorted([seeded.f1, seeded.f2, seeded.low])
    [row] = await read(
        app,
        "MATCH (a:Assessment {id:$id})-[:BELONGS_TO]->(i:Investigation) "
        "RETURN a.status AS status, a.classification AS classification, "
        "a.classification_rank AS rank, a.task_id AS task_id, a.attempt AS attempt, "
        "a.summary AS summary, i.id AS inv",
        {"id": resp.assessment_id},
    )
    assert row == {
        "status": "ACTIVE",
        "classification": "DEMO-MID",
        "rank": 2,  # max of DEMO-LOW (1) and DEMO-MID (2)
        "task_id": TASK,
        "attempt": 1,
        "summary": body["summary"],
        "inv": INV,
    }
    [sub] = await read(
        app,
        "MATCH (e:AuditEvent {operation:'assessments.create'})-[:CONCERNS]->(s:Submission) "
        "RETURN s.idempotency_key AS key, e.authz AS authz, e.outcome AS outcome",
    )
    assert sub == {"key": f"assessment:{TASK}", "authz": "ALLOWED", "outcome": "SUCCESS"}


async def test_citing_unretrieved_finding_rejected(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """AC7-s part 1: F3 exists and is visible, but this task never retrieved it."""
    await retrieve(client)
    geo = batch_body(INV, task_id="tsk_geo3", findings=[_geoint_finding("ENT-BRAVO")])
    [f3] = (await _post_ok(client, "/v1/findings:batch", geo, GEOINT))["finding_ids"]
    r = await client.post(URL, json=assessment_body([seeded.f1, f3]), headers=ANALYSIS_POST)
    assert r.status_code == 422, r.text
    assert ProblemDetails.model_validate(r.json()).code == "unknown_finding_reference"
    assert f3 not in r.text and seeded.f1 not in r.text
    assert (await _counts(app))["assessments"] == 0
    assert (await _counts(app))["submissions"] == 0
    rows = await read(
        app,
        "MATCH (e:AuditEvent {operation:'assessments.create'}) "
        "RETURN properties(e) AS p, e.authz AS authz, e.outcome AS outcome",
    )
    assert len(rows) == 1
    assert rows[0]["authz"] == "DENIED" and rows[0]["outcome"] == "DENIED"
    blob = repr(rows)
    assert "target_id" in rows[0]["p"]  # the blob really holds the properties
    for fid in (f3, seeded.f1):
        assert fid not in blob
    edges = await read(
        app, "MATCH (:AuditEvent {operation:'assessments.create'})-[r]->(f:Finding) RETURN f.id"
    )
    assert edges == []


async def test_citing_hidden_or_foreign_same_error(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await retrieve(client)
    bodies = []
    for fid in (seeded.high, seeded.foreign, "fnd_doesnotexist"):
        r = await client.post(URL, json=assessment_body([seeded.f1, fid]), headers=ANALYSIS_POST)
        assert r.status_code == 422, r.text
        assert fid not in r.text
        bodies.append(r.json())
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["code"] == "unknown_finding_reference"
    assert (await _counts(app))["assessments"] == 0


async def test_retrieved_then_reclassified_above_clearance_rejected(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """FR-211 "still visible": F2 was retrieved but is now above analysis clearance; the
    rejection is indistinguishable from citing a finding that was never retrieved."""
    await retrieve(client)
    geo = batch_body(INV, task_id="tsk_geo3", findings=[_geoint_finding("ENT-BRAVO")])
    [unretrieved] = (await _post_ok(client, "/v1/findings:batch", geo, GEOINT))["finding_ids"]
    r = await client.post(
        URL, json=assessment_body([seeded.f1, unretrieved]), headers=ANALYSIS_POST
    )
    assert r.status_code == 422, r.text
    not_retrieved_body = r.json()
    await read(app, "MATCH (e:AuditEvent {operation:'assessments.create'}) DETACH DELETE e")

    await app.state.lane.run(
        "test.reclassify",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.classification_rank = 3", {"id": seeded.f2}
        ),
    )
    r = await client.post(URL, json=assessment_body([seeded.f1, seeded.f2]), headers=ANALYSIS_POST)
    assert r.status_code == 422, r.text
    assert r.json() == not_retrieved_body
    assert r.json()["code"] == "unknown_finding_reference"
    assert seeded.f2 not in r.text and seeded.f1 not in r.text
    assert await _counts(app) == {"assessments": 0, "submissions": 0, "audits": 1}
    rows = await read(
        app,
        "MATCH (e:AuditEvent {operation:'assessments.create'}) "
        "RETURN properties(e) AS p, e.authz AS authz, e.outcome AS outcome",
    )
    assert rows[0]["authz"] == "DENIED" and rows[0]["outcome"] == "DENIED"
    assert "target_id" in rows[0]["p"]
    blob = repr(rows)
    assert seeded.f1 not in blob and seeded.f2 not in blob
    edges = await read(
        app, "MATCH (:AuditEvent {operation:'assessments.create'})-[r]->(f:Finding) RETURN f.id"
    )
    assert edges == []


async def test_cross_investigation_receipt_does_not_count(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """Review Focus 2: the same task retrieved INV_B's finding; citing it in INV is rejected."""
    await retrieve(client, INV)
    assert seeded.foreign in await retrieve(client, INV_B)
    r = await client.post(
        URL, json=assessment_body([seeded.f1, seeded.foreign]), headers=ANALYSIS_POST
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "unknown_finding_reference"
    assert (await _counts(app))["assessments"] == 0


async def test_same_body_replays_200_different_body_409(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await retrieve(client)
    first = await _post_ok(client, URL, assessment_body([seeded.f1]), ANALYSIS_POST)
    snapshot = await _counts(app)
    assert snapshot == {"assessments": 1, "submissions": 1, "audits": 1}

    r = await client.post(URL, json=assessment_body([seeded.f1], attempt=2), headers=ANALYSIS_POST)
    assert r.status_code == 200, r.text
    assert r.json() == first
    assert await _counts(app) == snapshot

    r = await client.post(
        URL, json=assessment_body([seeded.f1], summary="Changed."), headers=ANALYSIS_POST
    )
    assert r.status_code == 409, r.text
    problem = r.json()
    assert problem["code"] == "already_committed"
    assert problem["assessment_id"] == first["assessment_id"]
    assert await _counts(app) == snapshot


async def test_replay_after_retraction_returns_original(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """Review Focus 1: idempotency is checked before citations."""
    await retrieve(client)
    body = assessment_body([seeded.f1, seeded.f2])
    first = await _post_ok(client, URL, body, ANALYSIS_POST)
    await _set_finding_status(app, seeded.f1, "NEEDS_REVIEW")
    snapshot = await _counts(app)
    r = await client.post(URL, json=body, headers=ANALYSIS_POST)
    assert r.status_code == 200, r.text
    assert r.json() == first
    assert await _counts(app) == snapshot
    # Discriminating case: a citation check run first would now fail (F2 no longer visible at
    # rank 2); the replay must still win.
    await app.state.lane.run(
        "test.reclassify",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.classification_rank = 3", {"id": seeded.f2}
        ),
    )
    r = await client.post(
        URL, json=assessment_body([seeded.f1, seeded.f2], attempt=2), headers=ANALYSIS_POST
    )
    assert r.status_code == 200, r.text
    assert r.json() == first
    assert await _counts(app) == snapshot


async def test_retraction_race_writes_needs_review(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """Trust-a part 2 / FR-211a."""
    await retrieve(client)
    await _set_finding_status(app, seeded.f1, "NEEDS_REVIEW")
    resp = await _post_ok(client, URL, assessment_body([seeded.f1, seeded.f2]), ANALYSIS_POST)
    [row] = await read(
        app,
        "MATCH (a:Assessment {id:$id}) RETURN a.status AS status",
        {"id": resp["assessment_id"]},
    )
    assert row["status"] == "NEEDS_REVIEW"


async def test_receipt_spans_attempts(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await retrieve(client)  # retrieval made during attempt 1 of the task
    await _post_ok(client, URL, assessment_body([seeded.f1], attempt=2), ANALYSIS_POST)


async def test_closed_investigation_409(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await retrieve(client)
    r = await client.post(
        f"/v1/investigations/{INV}/close", json={"status": "CANCELLED"}, headers=ORCH
    )
    assert r.status_code == 200
    r = await client.post(URL, json=assessment_body([seeded.f1]), headers=ANALYSIS_POST)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "investigation_cancelled"
    assert await _counts(app) == {"assessments": 0, "submissions": 0, "audits": 0}


async def test_replay_after_close_returns_original(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """FR-210 "as the findings batch" (S2 G15): the replay check runs before the CLOSED fence, so
    a retry after close learns its committed assessment_id."""
    await retrieve(client)
    body = assessment_body([seeded.f1])
    first = await _post_ok(client, URL, body, ANALYSIS_POST)
    r = await client.post(
        f"/v1/investigations/{INV}/close", json={"status": "CANCELLED"}, headers=ORCH
    )
    assert r.status_code == 200
    snapshot = await _counts(app)
    r = await client.post(URL, json={**body, "attempt": 2}, headers=ANALYSIS_POST)
    assert r.status_code == 200, r.text
    assert r.json() == first
    assert await _counts(app) == snapshot


async def test_unknown_investigation_404(client: httpx.AsyncClient, seeded: Seeded) -> None:
    r = await client.post(
        URL, json=assessment_body([seeded.f1], inv="inv_nope"), headers=ANALYSIS_POST
    )
    assert r.status_code == 404, r.text


async def test_only_analysis_may_submit(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    r = await client.post(URL, json=assessment_body([seeded.f1]), headers=ORCH)
    assert r.status_code == 403, r.text
    assert (await _counts(app))["assessments"] == 0


# --- read model (FR-207) ------------------------------------------------------------------------


async def _context(client: httpx.AsyncClient, headers: dict[str, str]) -> InvestigationContext:
    r = await client.get(f"/v1/investigations/{INV}/context", headers=headers)
    assert r.status_code == 200, r.text
    return InvestigationContext.model_validate(r.json())


async def test_context_returns_the_assessment(client: httpx.AsyncClient, seeded: Seeded) -> None:
    await retrieve(client)
    body = assessment_body([seeded.f1, seeded.f2], contradiction=[seeded.f1, seeded.f2])
    resp = await _post_ok(client, URL, body, ANALYSIS_POST)
    for headers in (ORCH, analysis("tsk_later_read")):
        asm = (await _context(client, headers)).assessment
        assert asm is not None
        assert asm.assessment_id == resp["assessment_id"]
        assert asm.investigation_id == INV
        assert asm.status == "ACTIVE"
        submitted = AssessmentSubmission.model_validate(body)
        for field in (
            "summary",
            "conclusions",
            "contradictions",
            "information_gaps",
            "follow_up_questions",
            "model_id",
            "prompt_version",
        ):
            assert getattr(asm, field) == getattr(submitted, field), field


async def test_context_returns_the_latest_visible_status(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    await retrieve(client)
    first = await _post_ok(client, URL, assessment_body([seeded.f1]), ANALYSIS_POST)
    await retrieve(client, task="tsk_analysis_two")
    second = await _post_ok(
        client, URL, assessment_body([seeded.f2], task="tsk_analysis_two"), ANALYSIS_POST
    )
    asm = (await _context(client, ORCH)).assessment
    assert asm is not None and asm.assessment_id == second["assessment_id"]
    await _set_assessment(app, second["assessment_id"], "status", "NEEDS_REVIEW")
    asm = (await _context(client, ORCH)).assessment
    assert asm is not None and asm.assessment_id == second["assessment_id"]
    assert asm.status == "NEEDS_REVIEW"
    await _set_assessment(app, second["assessment_id"], "status", "SUPERSEDED")
    asm = (await _context(client, ORCH)).assessment
    assert asm is not None and asm.assessment_id == first["assessment_id"]


async def test_analysis_does_not_see_assessment_above_clearance(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seeded
) -> None:
    """FR-203 for assessments: their own classification_rank (FR-212)."""
    await retrieve(client)
    resp = await _post_ok(client, URL, assessment_body([seeded.f1]), ANALYSIS_POST)
    await _set_assessment(app, resp["assessment_id"], "classification_rank", 3)
    r = await client.get(f"/v1/investigations/{INV}/context", headers=analysis("tsk_later_read"))
    assert r.json()["assessment"] is None
    assert resp["assessment_id"] not in r.text
    asm = (await _context(client, ORCH)).assessment
    assert asm is not None and asm.assessment_id == resp["assessment_id"]


# --- helpers ------------------------------------------------------------------------------------


async def _set_finding_status(app: FastAPI, fid: str, status: str) -> None:
    await app.state.lane.run(
        "test.retract",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.status = $s", {"id": fid, "s": status}
        ),
    )


async def _set_assessment(app: FastAPI, aid: str, prop: str, value: Any) -> None:
    cypher = {
        "status": "MATCH (a:Assessment {id:$id}) SET a.status = $v",
        "classification_rank": "MATCH (a:Assessment {id:$id}) SET a.classification_rank = $v",
    }[prop]
    await app.state.lane.run("test.set", lambda conn: conn.execute(cypher, {"id": aid, "v": value}))
