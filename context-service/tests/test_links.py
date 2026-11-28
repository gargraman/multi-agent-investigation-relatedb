"""``GET /v1/investigations/{id}/links``: I4 cross-agent hidden links (S4a FR-218, I4-a, G7, G4).

The graph is the spike's seeded I4 pattern (``spikes/relate/tests/test_c6_queries.py`` fixtures)
mapped onto the baseline-corroboration manifest: ALPHA (a), CHARLIE (c, the ``via``), BRAVO (b).
Seeded **through ``findings:batch``**, which the validator allows for every finding here (a
SIGINT ``LINKED_TO`` between two handles, GEOINT ``PLACED_AT`` of a person at a cell). Direct
Cypher is used only for what the API cannot write: a ``DISPUTED`` status (an assessment drives it
in production), a manifest ``RELATED_TO`` edge (manifest-only, written at create).

Per G7 there is no "view at clearance" parameter, so the below-clearance cases call the
repository with ``max_rank=2``."""

from typing import Any

import httpx
from fastapi import FastAPI

from nexus_context_service.repository import GraphRepository, InvestigationNotFound
from nexus_contracts.api.graph import HiddenLink
from nexus_contracts.api.problem import ProblemDetails

from .conftest import GEOINT, ORCH, SIGINT, batch_body, create_body, evidence_ref, finding, read

INV = "inv_links"
INV_B = "inv_links_b"
OPERATOR = {**ORCH, "X-Nexus-Actor": "operator:demo"}

# Cell centres (pitch 100, radius 150): 00 (50,50), 07 (150,150) diagonal 141.4 away, 02 (250,50)
# is 200 away.
NEAR, FAR = "LOC-GRID-07", "LOC-GRID-02"


def links_url(inv: str = INV) -> str:
    return f"/v1/investigations/{inv}/links"


def _placed(person: str, cell: str, frm: str, until: str, evidence: str) -> dict[str, Any]:
    f = finding(evidence=(evidence,), classification="DEMO-MID", valid_from=frm, valid_until=until)
    f["evidence"] = [evidence_ref(evidence, owner="geoint-agent")]
    f["about"] = [person, cell]
    f["statement"] = f"{person} was placed at {cell}."
    f["prompt_version"] = "geoint-classify@1.0.0"
    f["assertions"] = [
        {
            "type": "PLACED_AT",
            "subject": person,
            "location": cell,
            "valid_from": frm,
            "valid_until": until,
        }
    ]
    return f


async def _submit(client: httpx.AsyncClient, inv: str, task: str, f: dict, who: dict) -> str:
    body = batch_body(inv, task, findings=[f])
    r = await client.post("/v1/findings:batch", json=body, headers=who)
    assert r.status_code == 201, r.text
    return r.json()["finding_ids"][0]


class Seed:
    """Finding ids of the seeded I4 pattern."""

    def __init__(self, fs: str, fc: str, fb: str) -> None:
        self.fs, self.fc, self.fb = fs, fc, fb


async def seed_path(
    client: httpx.AsyncClient,
    inv: str = INV,
    *,
    b_cell: str = NEAR,
    sigint_class: str = "DEMO-LOW",
) -> Seed:
    """A: HDL-ALPHA-1 <-> HDL-CHARLIE-1 contact (SIGINT); CHARLIE at cell 00 [10:00,12:00) and
    BRAVO at ``b_cell`` [11:00,13:00) (GEOINT): overlap [11:00,12:00)."""
    r = await client.post("/v1/investigations", json=create_body(inv), headers=ORCH)
    assert r.status_code == 201, r.text
    fs = await _submit(
        client,
        inv,
        f"tsk_sig_{inv}",
        finding(evidence=("ev-sig-001",), classification=sigint_class),
        SIGINT,
    )
    day = "2026-11-03"
    fc = await _submit(
        client,
        inv,
        f"tsk_geo_c_{inv}",
        _placed("ENT-CHARLIE", "LOC-GRID-00", f"{day}T10:00:00Z", f"{day}T12:00:00Z", "ev-geo-001"),
        GEOINT,
    )
    fb = await _submit(
        client,
        inv,
        f"tsk_geo_b_{inv}",
        _placed("ENT-BRAVO", b_cell, f"{day}T11:00:00Z", f"{day}T13:00:00Z", "ev-geo-002"),
        GEOINT,
    )
    return Seed(fs, fc, fb)


async def get_links(client: httpx.AsyncClient, inv: str = INV) -> list[HiddenLink]:
    r = await client.get(links_url(inv), headers=ORCH)
    assert r.status_code == 200, r.text
    return [HiddenLink.model_validate(x) for x in r.json()]


async def direct_contact(client: httpx.AsyncClient, classification: str = "DEMO-LOW") -> str:
    """A trusted SIGINT contact between ALPHA's and BRAVO's handles, through ``findings:batch``."""
    return await _submit(
        client,
        INV,
        "tsk_sig_direct",
        finding(
            "HDL-ALPHA-1",
            "HDL-BRAVO-1",
            evidence=("ev-sig-002",),
            classification=classification,
        ),
        SIGINT,
    )


def repo(app: FastAPI) -> GraphRepository:
    return app.state.repository


# --- the hidden path ----------------------------------------------------------------------------


async def test_links_returns_the_hidden_path(client: httpx.AsyncClient) -> None:
    seed = await seed_path(client)
    r = await client.get(links_url(), headers=ORCH)
    assert r.status_code == 200, r.text
    raw = r.json()
    # serialised by alias: the window key is ``from``, not ``from_``
    assert raw[0]["windows"][0].keys() == {"cell_c", "cell_b", "from", "until"}
    [link] = [HiddenLink.model_validate(x) for x in raw]
    assert (link.entity_a, link.via, link.entity_b) == ("ENT-ALPHA", "ENT-CHARLIE", "ENT-BRAVO")
    assert link.sigint_findings == [seed.fs]
    assert sorted(link.geoint_findings) == sorted([seed.fc, seed.fb])
    [w] = link.windows
    assert (w.cell_c, w.cell_b) == ("LOC-GRID-00", NEAR)
    assert (w.from_.isoformat(), w.until.isoformat()) == (
        "2026-11-03T11:00:00+00:00",
        "2026-11-03T12:00:00+00:00",
    )
    assert link.disputed is False


async def test_links_disputed_flag(client: httpx.AsyncClient, app: FastAPI) -> None:
    """A DISPUTED supporting finding is still trusted and flips ``disputed``. DISPUTED is set by
    direct Cypher (in production a contradicting assessment sets it, Task 6)."""
    seed = await seed_path(client)
    await app.state.lane.run(
        "test.dispute",
        lambda conn: conn.execute(
            "MATCH (f:Finding {id:$id}) SET f.status = 'DISPUTED'", {"id": seed.fc}
        ),
    )
    [link] = await get_links(client)
    assert link.disputed is True


# --- the ADR-007 negatives: each is [] ----------------------------------------------------------


async def test_links_gone_after_retraction(client: httpx.AsyncClient) -> None:
    await seed_path(client)
    assert len(await get_links(client)) == 1
    r = await client.post(
        f"/v1/investigations/{INV}/evidence/ev-sig-001/retract",
        json={"reason": "tampered"},
        headers=OPERATOR,
    )
    assert r.status_code == 201, r.text
    assert await get_links(client) == []


async def test_links_gone_with_direct_contact(client: httpx.AsyncClient) -> None:
    await seed_path(client)
    await direct_contact(client)
    assert await get_links(client) == []


async def test_links_gone_with_manifest_relation(client: httpx.AsyncClient, app: FastAPI) -> None:
    """Manifest ``RELATED_TO`` ALPHA-BRAVO, by direct Cypher (manifest edges: create only)."""
    await seed_path(client)
    await app.state.lane.run(
        "test.relate",
        lambda conn: conn.execute(
            "MATCH (a:Entity {uid:$a}), (b:Entity {uid:$b}) "
            "CREATE (a)-[:RELATED_TO {type:'KNOWS'}]->(b)",
            {"a": f"{INV}:ENT-ALPHA", "b": f"{INV}:ENT-BRAVO"},
        ),
    )
    assert await get_links(client) == []


async def test_links_gone_when_far(client: httpx.AsyncClient) -> None:
    await seed_path(client, b_cell=FAR)
    assert await get_links(client) == []


# --- clearance (repository level, G7) -----------------------------------------------------------


async def test_links_high_direct_contact_at_mid_rank_keeps_path(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """A DEMO-HIGH direct contact hides the path from an orchestrator (rank 3) but must neither
    suppress it nor leak at rank 2 (a hidden finding is excluded from the computation)."""
    await seed_path(client)
    high = await direct_contact(client, "DEMO-HIGH")
    assert await get_links(client) == []  # rank 3 sees the contact
    [link] = await repo(app).links(INV, max_rank=2)
    assert (link.entity_a, link.via, link.entity_b) == ("ENT-ALPHA", "ENT-CHARLIE", "ENT-BRAVO")
    assert high not in link.model_dump_json(by_alias=True)


async def test_links_hidden_contact_leg_is_not_a_path_at_mid_rank(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """The SIGINT leg itself is DEMO-HIGH: rank 3 sees the path, rank 2 sees nothing and no id."""
    seed = await seed_path(client, sigint_class="DEMO-HIGH")
    [link] = await repo(app).links(INV, max_rank=3)
    assert link.sigint_findings == [seed.fs]
    assert await repo(app).links(INV, max_rank=2) == []


# --- scoping, errors, auth ----------------------------------------------------------------------


async def test_links_do_not_mix_investigations(client: httpx.AsyncClient) -> None:
    await seed_path(client, INV)
    await seed_path(client, INV_B)
    await _submit(
        client,
        INV_B,
        "tsk_sig_direct_b",
        finding("HDL-ALPHA-1", "HDL-BRAVO-1", evidence=("ev-sig-002",)),
        SIGINT,
    )
    assert len(await get_links(client, INV)) == 1  # B's direct contact does not suppress A's path
    assert await get_links(client, INV_B) == []


async def test_links_unknown_investigation_is_404(client: httpx.AsyncClient, app: FastAPI) -> None:
    r = await client.get(links_url("inv_nope"), headers=ORCH)
    assert r.status_code == 404
    ProblemDetails.model_validate(r.json())
    try:
        await repo(app).links("inv_nope", max_rank=3)
    except InvestigationNotFound:
        pass
    else:
        raise AssertionError("expected InvestigationNotFound")


async def test_links_empty_investigation_is_empty_list(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/investigations", json=create_body(INV), headers=ORCH)
    assert r.status_code == 201, r.text
    assert await get_links(client) == []


async def test_links_orchestrator_only_and_writes_no_audit(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    await seed_path(client)
    assert (await client.get(links_url(), headers=SIGINT)).status_code == 403
    assert (await client.get(links_url(), headers=GEOINT)).status_code == 403
    assert (await client.get(links_url())).status_code == 401
    assert (await client.get(links_url(), headers=ORCH)).status_code == 200
    # a read is not in FR-204's audited list; only the denials are written
    rows = await read(
        app,
        "MATCH (a:AuditEvent {operation:'investigations.links'}) "
        "RETURN a.authz AS authz ORDER BY a.at",
    )
    assert [r["authz"] for r in rows] == ["DENIED", "DENIED"]
