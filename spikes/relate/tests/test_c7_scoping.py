"""C7: per-investigation scoping (ADR-007 G5) — two runs of the same scenario coexist."""

import pytest
import relate

import queries as Q
from common import RADIUS, open_seeded, seed_investigation


def test_two_investigations_same_manifest_coexist(store):
    conn = open_seeded(store, invs=("inv1", "inv2"))
    per_inv = conn.execute(
        "MATCH (i:Investigation)-[:HAS_ENTITY]->(e:Entity) RETURN i.id AS inv, count(e) AS n, "
        "collect(DISTINCT e.manifest_id) AS mids ORDER BY inv"
    ).fetchall()
    print("\nentities per investigation:", [(r["inv"], r["n"]) for r in per_inv])
    assert [r["n"] for r in per_inv] == [10, 10]
    assert sorted(per_inv[0]["mids"]) == sorted(per_inv[1]["mids"])  # same manifest ids in both
    assert conn.execute("MATCH (e:Entity {manifest_id:'A'}) RETURN count(e) AS n").single().n == 2

    for inv in ("inv1", "inv2"):
        rows = conn.execute(Q.I4, {"inv": inv, "max_rank": 1, "radius": RADIUS}).fetchall()
        print(f"I4[{inv}]:", [(r["entity_a"], r["via"], r["entity_b"], r["sigint_findings"]) for r in rows])
        assert [(r["entity_a"], r["via"], r["entity_b"]) for r in rows] == [("A", "C", "B")]
        assert rows[0]["sigint_findings"] == [f"{inv}:fs"]
        i3 = conn.execute(Q.I3, {"uid": f"{inv}:C"}).fetchall()
        assert {r["f1"].properties["investigation_id"] for r in i3} == {inv}

    # Retracting in inv1 does not affect inv2
    conn.execute("MATCH (f:Finding {id:'inv1:fs'}) SET f.status = 'NEEDS_REVIEW'")
    assert conn.execute(Q.I4, {"inv": "inv1", "max_rank": 1, "radius": RADIUS}).fetchall() == []
    assert len(conn.execute(Q.I4, {"inv": "inv2", "max_rank": 1, "radius": RADIUS}).fetchall()) == 1
    conn.close()


def test_reseeding_same_investigation_is_rejected_atomically(store):
    conn = open_seeded(store, invs=("inv1",))
    before = conn.execute("MATCH (n) RETURN count(n) AS n").single().n
    conn.autocommit = False
    with pytest.raises(relate.IntegrityError) as ei:
        seed_investigation(conn, "inv1")
    conn.rollback()
    conn.autocommit = True
    after = conn.execute("MATCH (n) RETURN count(n) AS n").single().n
    print(f"\nre-seed inv1: {ei.value}; nodes before={before} after={after}")
    assert before == after
    conn.close()
