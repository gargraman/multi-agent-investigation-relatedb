"""I1-I4 queries exactly as sketched in spec §10 / ADR-007 §3 (verbatim where the sketch is complete)."""

# I1 — spec §10, verbatim
I1 = "MATCH p=(a:Assessment {id:$id})-[:USES_FINDING]->(f)-[:SUPPORTED_BY]->(e) RETURN p"

# I2 — spec §10 sketch completed (the sketch ends in '...'); quantified path kept verbatim
I2 = """
MATCH (e:Evidence {uid:$uid})<-[:SUPPORTED_BY]-(f)
OPTIONAL MATCH (f)<-[:DERIVED_FROM]-+(g)
WITH e, collect(DISTINCT f) + collect(DISTINCT g) AS impacted
UNWIND impacted AS x
OPTIONAL MATCH (a:Assessment)-[:USES_FINDING]->(x)
OPTIONAL MATCH (x)-[:ASSERTS]->(s:Assertion)
RETURN collect(DISTINCT x.id) AS findings, collect(DISTINCT a.id) AS assessments,
       collect(DISTINCT s.id) AS assertions
"""

# I3 — spec §10, verbatim
I3 = "MATCH (f1)-[:ABOUT]->(x:Entity {uid:$uid})<-[:ABOUT]-(f2) WHERE (f1)-[:CONTRADICTS]-(f2) RETURN f1,f2"

# I4 — ADR-007 §3, verbatim
I4 = """
MATCH (inv:Investigation {id: $inv})

// Hop 1, SIGINT: A's handle in contact with C's handle (undirected)
MATCH (inv)-[:CONTAINS]->(fs:Finding)-[:ASSERTS]->(l:Assertion {type: 'LINKED_TO'}),
      (l)-[:SUBJECT|OBJECT]->(ha:Entity)-[:RELATED_TO {type: 'USED_BY'}]->(a:Entity),
      (l)-[:SUBJECT|OBJECT]->(hc:Entity)-[:RELATED_TO {type: 'USED_BY'}]->(c:Entity)
WHERE ha <> hc AND a <> c
  AND fs.status IN ['ACTIVE', 'DISPUTED'] AND fs.classification_rank <= $max_rank

// Hop 2, GEOINT: C and B in neighbouring cells, overlapping windows
MATCH (inv)-[:CONTAINS]->(fc:Finding)-[:ASSERTS]->(pc:Assertion {type: 'PLACED_AT'})-[:SUBJECT]->(c),
      (pc)-[:AT]->(lc:Location)
MATCH (inv)-[:CONTAINS]->(fb:Finding)-[:ASSERTS]->(pb:Assertion {type: 'PLACED_AT'})-[:SUBJECT]->(b:Entity),
      (pb)-[:AT]->(lb:Location)
WHERE b <> a AND b <> c
  AND fs.source_agent_id <> fc.source_agent_id
  AND all(f IN [fc, fb] WHERE f.status IN ['ACTIVE', 'DISPUTED'] AND f.classification_rank <= $max_rank)
  AND point.distance(lc.position, lb.position) <= $radius
  AND pc.valid_from < pb.valid_until AND pb.valid_from < pc.valid_until

// Hidden: no trusted direct A–B link of any kind (G4)
  AND NOT EXISTS { (a)-[:RELATED_TO]-(b) }
  AND NOT EXISTS {
    MATCH (inv)-[:CONTAINS]->(fx:Finding)-[:ASSERTS]->(x:Assertion {type: 'LINKED_TO'}),
          (x)-[:SUBJECT|OBJECT]->(:Entity)-[:RELATED_TO {type: 'USED_BY'}]->(a),
          (x)-[:SUBJECT|OBJECT]->(:Entity)-[:RELATED_TO {type: 'USED_BY'}]->(b)
    WHERE fx.status IN ['ACTIVE', 'DISPUTED'] AND fx.classification_rank <= $max_rank
  }
  AND NOT EXISTS {
    MATCH (inv)-[:CONTAINS]->(f1:Finding)-[:ASSERTS]->(p1:Assertion {type: 'PLACED_AT'})-[:SUBJECT]->(a),
          (p1)-[:AT]->(l1:Location),
          (inv)-[:CONTAINS]->(f2:Finding)-[:ASSERTS]->(p2:Assertion {type: 'PLACED_AT'})-[:SUBJECT]->(b),
          (p2)-[:AT]->(l2:Location)
    WHERE all(f IN [f1, f2] WHERE f.status IN ['ACTIVE', 'DISPUTED'] AND f.classification_rank <= $max_rank)
      AND point.distance(l1.position, l2.position) <= $radius
      AND p1.valid_from < p2.valid_until AND p2.valid_from < p1.valid_until
  }

WITH a, c, b, fs, fc, fb, lc, lb,
     CASE WHEN pc.valid_from > pb.valid_from THEN pc.valid_from ELSE pb.valid_from END AS overlap_from,
     CASE WHEN pc.valid_until < pb.valid_until THEN pc.valid_until ELSE pb.valid_until END AS overlap_until
WITH a, c, b,
     collect(DISTINCT fs.id) AS sigint_findings,
     collect(DISTINCT fc.id) + collect(DISTINCT fb.id) AS geo_raw,
     collect(DISTINCT {cell_c: lc.manifest_id, cell_b: lb.manifest_id,
                       from: overlap_from, until: overlap_until}) AS windows,
     max(CASE WHEN 'DISPUTED' IN [fs.status, fc.status, fb.status] THEN 1 ELSE 0 END) AS any_disputed
RETURN a.manifest_id AS entity_a, c.manifest_id AS via, b.manifest_id AS entity_b,
       sigint_findings,
       reduce(acc = [], x IN geo_raw | CASE WHEN x IN acc THEN acc ELSE acc + x END) AS geoint_findings,
       windows,
       any_disputed = 1 AS disputed
"""
