"""``GET /v1/investigations/{id}/context`` read model Cypher (S2 spec FR-044, G5).

Flat row queries assembled in Python (one read transaction on the lane), rather than nested
collect subqueries: simpler to reason about on Relate and each query stays a plain pattern.
"""

FINDINGS = """
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)
RETURN f.id AS id, f.kind AS kind, f.status AS status, f.statement AS statement,
       f.confidence AS confidence, f.confidence_band AS confidence_band,
       f.classification AS classification, f.source_node_id AS source_node_id,
       f.task_id AS task_id, f.attempt AS attempt, f.model_id AS model_id,
       f.prompt_version AS prompt_version, f.valid_from AS valid_from,
       f.valid_until AS valid_until, f.created_at AS created_at
ORDER BY f.created_at, f.id
"""

ABOUT = """
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)-[:ABOUT]->(e:Entity)
RETURN f.id AS fid, e.manifest_id AS manifest_id
"""

EVIDENCE = """
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)-[r:SUPPORTED_BY]->(e:Evidence)
RETURN f.id AS fid, e.manifest_id AS evidence_id, r.sha256 AS sha256, r.summary AS summary
"""

LINKED_TO = """
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)-[:ASSERTS]->(a:Assertion)
WHERE a.type = 'LINKED_TO'
MATCH (a)-[:SUBJECT]->(s:Entity), (a)-[:OBJECT]->(o:Entity)
RETURN f.id AS fid, s.manifest_id AS subject, o.manifest_id AS object,
       a.link_type AS link_type, a.valid_from AS valid_from, a.valid_until AS valid_until
"""

PLACED_AT = """
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)-[:ASSERTS]->(a:Assertion)
WHERE a.type = 'PLACED_AT'
MATCH (a)-[:SUBJECT]->(s:Entity), (a)-[:AT]->(l:Location)
RETURN f.id AS fid, s.manifest_id AS subject, l.manifest_id AS location,
       a.valid_from AS valid_from, a.valid_until AS valid_until
"""
