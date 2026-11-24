"""C1: constraint, index and point-index DDL as spec §9.3 assumes."""

import relate
import pytest

from common import SCHEMA_DDL, bootstrap, cell_center


def test_bootstrap_idempotent_and_listed(store):
    conn = relate.connect(store)
    bootstrap(conn)
    bootstrap(conn)  # IF NOT EXISTS: second run must be a no-op, no error
    cons = conn.execute("SHOW CONSTRAINTS").fetchall()
    idx = conn.execute("SHOW INDEXES").fetchall()
    print("\nconstraints:", sorted((c["name"], c["type"], tuple(c["labelsOrTypes"]), tuple(c["properties"])) for c in cons))
    print("indexes:", sorted((i["name"], i["type"]) for i in idx))
    assert len(cons) == 10
    assert all(c["type"] == "NODE_PROPERTY_UNIQUENESS" for c in cons)
    by_name = {i["name"]: i for i in idx}
    assert by_name["location_position"]["type"] == "POINT"
    assert by_name["entity_manifest"]["type"] == "RANGE"
    assert all(i["state"] == "ONLINE" for i in idx)
    conn.close()
    # Schema survives reopen
    conn = relate.connect(store)
    assert len(conn.execute("SHOW CONSTRAINTS").fetchall()) == 10
    assert any(i["type"] == "POINT" for i in conn.execute("SHOW INDEXES").fetchall())
    conn.close()


def test_unique_uid_rejects_duplicates(store):
    conn = relate.connect(store)
    bootstrap(conn)
    conn.execute("CREATE (:Entity {uid:'inv1:A'})")
    # autocommit duplicate
    with pytest.raises(relate.IntegrityError) as ei:
        conn.execute("CREATE (:Entity {uid:'inv1:A'})")
    print("\nautocommit dup:", type(ei.value).__name__, ei.value, ei.value.neo4j_code)
    # duplicate via a multi-label node (Entity:Location) is also caught
    with pytest.raises(relate.IntegrityError):
        conn.execute("CREATE (:Entity:Location {uid:'inv1:A'})")
    # duplicate inside one explicit transaction (both in the same tx)
    conn.autocommit = False
    conn.execute("CREATE (:Evidence {uid:'inv1:ev'})")
    with pytest.raises(relate.IntegrityError) as ei2:
        conn.execute("CREATE (:Evidence {uid:'inv1:ev'})")
    print("in-tx dup:", ei2.value)
    conn.rollback()
    conn.autocommit = True
    assert conn.execute("MATCH (e:Evidence) RETURN count(e) AS n").single().n == 0
    # Submission.idempotency_key
    conn.execute("CREATE (:Submission {id:'s1', idempotency_key:'task-1'})")
    with pytest.raises(relate.IntegrityError):
        conn.execute("CREATE (:Submission {id:'s2', idempotency_key:'task-1'})")
    # MERGE on the key is the idempotent path
    conn.execute("MERGE (:Entity {uid:'inv1:A'})")
    assert conn.execute("MATCH (e:Entity {uid:'inv1:A'}) RETURN count(e) AS n").single().n == 1
    conn.close()


def test_cartesian_point_and_point_index_plan(store):
    conn = relate.connect(store)
    bootstrap(conn)
    rows = [{"uid": f"L{c}_{r}", "pos": cell_center(c, r)} for c in range(60) for r in range(60)]
    conn.execute("UNWIND $rows AS r CREATE (:Entity:Location {uid:r.uid, position:r.pos})", {"rows": rows})
    p = conn.execute("MATCH (l:Location {uid:'L2_2'}) RETURN l.position AS p").single().p
    print("\nstored point:", p, "srid", p.srid, "crs", p.crs)
    assert p.srid == 7203
    q = "MATCH (l:Location) WHERE point.distance(l.position, $p) <= 1.5 RETURN count(l) AS n"
    n = conn.execute(q, {"p": cell_center(2, 2)}).single().n
    assert n == 9  # the cell + 8 neighbours (diagonal = sqrt(2) ~ 1.41)
    plan = conn.execute("PROFILE " + q, {"p": cell_center(2, 2)}).consume().plan

    def ops(node):
        yield node["operator"], node.get("details")
        for ch in node.get("children", []):
            yield from ops(ch)

    operators = list(ops(plan["root"]))
    print("PROFILE operators (3600 Locations):", operators)
    bbox = conn.execute(
        "PROFILE MATCH (l:Location) WHERE point.withinBBox(l.position, point({x:1.0,y:1.0}), point({x:4.0,y:4.0})) RETURN count(l) AS n"
    ).consume().plan
    print("withinBBox operators:", list(ops(bbox["root"])))
    conn.close()
