"""C6: I1-I4 sketches on a seeded graph + I4 negative cases + Cypher feature matrix."""

import pytest
import relate

import queries as Q
from common import RADIUS, dt, open_seeded


@pytest.fixture
def db(store):
    conn = open_seeded(store)
    yield conn
    conn.close()


def i4(conn, max_rank=1, inv="inv1"):
    return conn.execute(Q.I4, {"inv": inv, "max_rank": max_rank, "radius": RADIUS}).fetchall()


def test_i1_provenance(db):
    rows = db.execute(Q.I1, {"id": "inv1:as1"}).fetchall()
    paths = [r["p"] for r in rows]
    print("\nI1 paths:", [[n.get("id") or n.get("uid") for n in r.data()["p"] if isinstance(n, dict)] for r in rows])
    assert len(paths) == 2 and all(isinstance(p, relate.Path) for p in paths)


def test_i2_quantified_path_impact(db):
    r = db.execute(Q.I2, {"uid": "inv1:ev_sig"}).single()
    print("\nI2:", dict(r))
    assert sorted(r["findings"]) == ["inv1:fs", "inv1:g1", "inv1:g2"]  # depth 0, 1, 2
    assert sorted(r["assessments"]) == ["inv1:as1", "inv1:as2"]
    assert r["assertions"] == ["inv1:l1"]


def test_i3_contradiction_neighbourhood(db):
    rows = db.execute(Q.I3, {"uid": "inv1:C"}).fetchall()
    pairs = sorted((r["f1"].properties["id"], r["f2"].properties["id"]) for r in rows)
    print("\nI3:", pairs)
    assert pairs == [("inv1:k1", "inv1:k2"), ("inv1:k2", "inv1:k1")]


def test_i4_returns_exactly_a_c_b(db):
    rows = i4(db)
    print("\nI4:", [dict(r) for r in rows])
    assert [(r["entity_a"], r["via"], r["entity_b"]) for r in rows] == [("A", "C", "B")]
    r = rows[0]
    assert r["sigint_findings"] == ["inv1:fs"]
    assert sorted(r["geoint_findings"]) == ["inv1:fb", "inv1:fc"]  # reduce de-dup
    assert r["windows"] == [{"cell_c": "cell_2_2", "cell_b": "cell_3_3", "from": dt(11), "until": dt(12)}]
    assert r["disputed"] is False


def test_i4_disputed_flag(db):
    db.execute("MATCH (f:Finding {id:'inv1:fc'}) SET f.status = 'DISPUTED'")
    rows = i4(db)
    assert len(rows) == 1 and rows[0]["disputed"] is True


def test_i4_sigint_needs_review_removes_path(db):
    """Retraction (I2) flow as one transaction: evidence RETRACTED, dependent findings NEEDS_REVIEW,
    Decision + AuditEvent — then I4 must drop the path with no extra writes to the Assertion."""
    db.autocommit = False
    impact = db.execute(Q.I2, {"uid": "inv1:ev_sig"}).single()
    db.execute("MATCH (e:Evidence {uid:$uid}) SET e.status = 'RETRACTED'", {"uid": "inv1:ev_sig"})
    db.execute("MATCH (f:Finding) WHERE f.id IN $ids SET f.status = 'NEEDS_REVIEW'", {"ids": impact["findings"]})
    db.execute(
        "MATCH (e:Evidence {uid:$uid}) CREATE (d:Decision {id:'d1', actor:'operator:demo'})-[:DISPOSES]->(e) "
        "CREATE (:AuditEvent {id:'ae1', op:'retract_evidence'})-[:CONCERNS]->(d)",
        {"uid": "inv1:ev_sig"},
    )
    db.commit()
    db.autocommit = True
    rows = i4(db)
    print("\nI4 after SIGINT finding NEEDS_REVIEW:", rows)
    assert rows == []


def _add_direct_contact(conn, rank):
    conn.autocommit = False
    conn.execute(
        "MATCH (inv:Investigation {id:'inv1'}), (e:Evidence {uid:'inv1:ev_sig'}) "
        "CREATE (f:Finding {id:'inv1:fd', investigation_id:'inv1', source_agent_id:'sigint-1', status:'ACTIVE', "
        "classification_rank:$rank}) CREATE (inv)-[:CONTAINS]->(f) CREATE (f)-[:SUPPORTED_BY]->(e)",
        {"rank": rank},
    )
    conn.execute(
        "MATCH (f:Finding {id:'inv1:fd'}), (s:Entity {uid:'inv1:hA'}), (o:Entity {uid:'inv1:hB'}) "
        "CREATE (f)-[:ASSERTS]->(a:Assertion {id:'inv1:ld', type:'LINKED_TO', link_type:'CONTACT', "
        "valid_from:$vf, valid_until:$vu}) CREATE (a)-[:SUBJECT]->(s) CREATE (a)-[:OBJECT]->(o)",
        {"vf": dt(9), "vu": dt(10)},
    )
    conn.commit()
    conn.autocommit = True


def test_i4_trusted_direct_contact_removes_path(db):
    _add_direct_contact(db, rank=1)
    rows = i4(db)
    print("\nI4 after trusted direct A-B handle contact:", rows)
    assert rows == []


def test_i4_over_clearance_direct_contact_does_not_suppress(db):
    _add_direct_contact(db, rank=3)  # DEMO-HIGH
    rows = i4(db, max_rank=2)  # caller at DEMO-MID
    assert [(r["entity_a"], r["via"], r["entity_b"]) for r in rows] == [("A", "C", "B")]
    assert "inv1:fd" not in str([dict(r) for r in rows])


def test_i4_direct_manifest_relation_removes_path(db):
    db.execute("MATCH (a:Entity {uid:'inv1:A'}), (b:Entity {uid:'inv1:B'}) CREATE (a)-[:RELATED_TO {type:'KNOWS'}]->(b)")
    assert i4(db) == []


def test_i4_two_cells_apart_is_not_colocated(db):
    db.execute("MATCH (l:Location {uid:'inv1:cell_3_3'}) SET l.position = point({x: 4.5, y: 2.5})")  # 2 cells away
    assert i4(db) == []


# --- Cypher feature matrix ---------------------------------------------------
FEATURES = [
    ("EXISTS { pattern }", "MATCH (a:Entity {uid:'inv1:hA'}) RETURN EXISTS { (a)-[:RELATED_TO]->(:Entity) } AS v", True),
    ("NOT EXISTS { MATCH ... WHERE ... }",
     "MATCH (a:Entity {uid:'inv1:A'}) RETURN NOT EXISTS { MATCH (f:Finding)-[:ABOUT]->(a) WHERE f.status = 'ACTIVE' } AS v", True),
    ("[:A|B] type alternation", "MATCH (:Assertion {id:'inv1:l1'})-[:SUBJECT|OBJECT]->(h) RETURN count(h) AS v", 2),
    ("pattern predicate in WHERE (I3)",
     "MATCH (f1:Finding {id:'inv1:k1'}), (f2:Finding {id:'inv1:k2'}) WHERE (f1)-[:CONTRADICTS]-(f2) RETURN count(*) AS v", 1),
    ("cartesian point.distance", "RETURN point.distance(point({x:2.5,y:2.5}), point({x:3.5,y:3.5})) <= 1.5 AS v", True),
    ("point.distance on stored props",
     "MATCH (a:Location {uid:'inv1:cell_2_2'}), (b:Location {uid:'inv1:cell_3_3'}) RETURN round(point.distance(a.position,b.position),4) AS v", 1.4142),
    ("datetime comparison (stored)",
     "MATCH (a:Assertion {id:'inv1:pc'}), (b:Assertion {id:'inv1:pb'}) RETURN a.valid_from < b.valid_until AND b.valid_from < a.valid_until AS v", True),
    ("datetime() literal vs param", "RETURN datetime('2026-11-01T10:00:00Z') = $t AS v", True),
    ("reduce de-dup", "RETURN reduce(acc = [], x IN ['a','b','a'] | CASE WHEN x IN acc THEN acc ELSE acc + x END) AS v", ["a", "b"]),
    ("map literal in collect(DISTINCT {...})",
     "UNWIND [1,1,2] AS i RETURN size(collect(DISTINCT {k: i, s: 'x'})) AS v", 2),
    ("all(... IN [n1,n2] WHERE ...)",
     "MATCH (f1:Finding {id:'inv1:fc'}), (f2:Finding {id:'inv1:fb'}) RETURN all(f IN [f1,f2] WHERE f.status IN ['ACTIVE','DISPUTED']) AS v", True),
    ("quantified path <-[:DERIVED_FROM]-+",
     "MATCH (:Finding {id:'inv1:fs'})<-[:DERIVED_FROM]-+(g) RETURN count(g) AS v", 2),
    ("quantified group {1,}",
     "MATCH (:Finding {id:'inv1:fs'}) (()<-[:DERIVED_FROM]-()){1,} (g) RETURN count(g) AS v", 2),
    ("var-length *1..", "MATCH (:Finding {id:'inv1:fs'})<-[:DERIVED_FROM*1..]-(g) RETURN count(g) AS v", 2),
    ("COUNT { } subquery", "MATCH (f:Finding {id:'inv1:fb'}) RETURN COUNT { (f)-[:ASSERTS]->() } AS v", 3),
    ("COLLECT { } subquery",
     "MATCH (f:Finding {id:'inv1:fs'}) RETURN COLLECT { MATCH (f)<-[:DERIVED_FROM]-(g) RETURN g.id } AS v", ["inv1:g1"]),
    ("max(CASE ...) aggregate + list IN", "UNWIND ['ACTIVE','DISPUTED'] AS s RETURN max(CASE WHEN 'DISPUTED' IN [s] THEN 1 ELSE 0 END) AS v", 1),
    ("collect(DISTINCT) + collect(DISTINCT)", "UNWIND [1,2,2] AS i RETURN collect(DISTINCT i) + collect(DISTINCT i*10) AS v", [1, 2, 10, 20]),
    ("shortestPath", "MATCH p = shortestPath((a:Entity {uid:'inv1:hA'})-[*..6]-(b:Entity {uid:'inv1:B'})) RETURN length(p) AS v", None),
    ("CALL { } IN TRANSACTIONS", "UNWIND range(1,3) AS i CALL { WITH i CREATE (:Tmp {i:i}) } IN TRANSACTIONS OF 2 ROWS RETURN count(*) AS v", 3),
]


def test_pattern_expression_in_return_rejected(db):
    """Recorded behaviour (matches Neo4j 5 / Cypher 25): pattern predicates only in WHERE; use EXISTS {} elsewhere."""
    with pytest.raises(relate.ProgrammingError) as ei:
        db.execute("MATCH (f1:Finding {id:'inv1:k1'}), (f2:Finding {id:'inv1:k2'}) RETURN (f1)-[:CONTRADICTS]-(f2) AS v")
    print("\npattern expr in RETURN:", ei.value)
    v = db.execute("MATCH (f1:Finding {id:'inv1:k1'}), (f2:Finding {id:'inv1:k2'}) "
                   "RETURN EXISTS { (f1)-[:CONTRADICTS]-(f2) } AS v").single().v
    assert v is True


@pytest.mark.parametrize("name,query,expected", FEATURES, ids=[f[0] for f in FEATURES])
def test_cypher_feature(db, name, query, expected):
    try:
        v = db.execute(query, {"t": dt(10)}).single().v
    except relate.Error as e:
        print(f"\nFEATURE {name}: ERROR {type(e).__name__}: {e}")
        raise
    print(f"\nFEATURE {name}: OK -> {v!r}")
    if expected is not None:
        assert v == expected
