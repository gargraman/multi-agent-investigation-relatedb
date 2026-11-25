"""``POST /v1/findings:batch`` Cypher (S2 spec §8.4, spec §9.1–§9.3, ADR-001 R1/R2, ADR-002).

All statements run inside **one** lane transaction; any error rolls the whole batch back.

G16 (datetimes nested in UNWIND map parameters) was probed on relate-db 1.2.0 before this was
written: maps carrying ``datetime`` values UNWIND correctly, are stored as Cypher temporal values
(not strings; ``>=`` against a ``datetime`` parameter and against ``datetime('…')`` both work),
round-trip with microseconds and tz=UTC, and ``null`` map values are accepted. So findings,
ABOUT edges, evidence links and assertions are each written with one UNWIND statement, not one
statement per row. Every statement returns a row count that the repository checks, so a
silently-unmatched ``MATCH`` (which would drop an edge) fails the transaction instead.
"""

# 1. Idempotency + fence in one read. The replay decision is taken before the fence (G15).
CHECK_SUBMISSION = """
MATCH (i:Investigation {id: $inv})
OPTIONAL MATCH (s:Submission {idempotency_key: $key})
RETURN i.write_state AS write_state, s.content_hash AS content_hash,
       s.finding_ids AS finding_ids, s.investigation_id AS sub_inv, s.caller AS sub_caller
"""

# 2a. Entities referenced by about / assertions, scoped to this investigation (HAS_ENTITY).
GET_ENTITIES = """
MATCH (i:Investigation {id: $inv})-[:HAS_ENTITY]->(e:Entity)
WHERE e.manifest_id IN $entity_ids
RETURN e.manifest_id AS manifest_id, e.type AS type
"""

# 2b. Evidence referenced by the batch, scoped to this investigation (HAS_EVIDENCE).
GET_EVIDENCE = """
MATCH (i:Investigation {id: $inv})-[:HAS_EVIDENCE]->(ev:Evidence)
WHERE ev.manifest_id IN $evidence_ids
RETURN ev.manifest_id AS evidence_id, ev.sha256 AS sha256, ev.owner_node_id AS owner,
       ev.classification AS classification, ev.status AS status
"""

# 3. The Submission (unique idempotency_key backs the replay check at the store level too).
CREATE_SUBMISSION = """
CREATE (:Submission {
  id: $submission_id, idempotency_key: $key, content_hash: $content_hash,
  investigation_id: $inv, task_id: $task_id, attempt: $attempt, caller: $caller,
  finding_ids: $finding_ids, committed_at: $now
})
"""

# 4. Findings (server-set provenance: id, source ids, status, version, created_at).
CREATE_FINDINGS = """
MATCH (i:Investigation {id: $inv}), (s:Submission {id: $submission_id})
UNWIND $findings AS f
CREATE (n:Finding {
  id: f.id, investigation_id: $inv, submission_id: $submission_id, kind: f.kind,
  statement: f.statement, confidence: f.confidence, confidence_band: f.confidence_band,
  classification: f.classification, classification_rank: f.classification_rank,
  source_agent_id: $caller, source_node_id: $caller, source_evidence_ids: f.evidence_ids,
  task_id: $task_id, attempt: $attempt, model_id: f.model_id, prompt_version: f.prompt_version,
  schema_version: f.schema_version, reasoning_summary: f.reasoning_summary,
  valid_from: f.valid_from, valid_until: f.valid_until, status: 'ACTIVE', version: 1,
  supersedes_id: null, synthetic: f.synthetic, created_at: $now
})
CREATE (i)-[:CONTAINS]->(n)
CREATE (s)-[:COMMITTED]->(n)
RETURN count(n) AS created
"""

# 4b. ABOUT edges: rows [{fid, uid}].
CREATE_ABOUT = """
UNWIND $rows AS r
MATCH (f:Finding {id: r.fid}), (x:Entity {uid: r.uid})
CREATE (f)-[:ABOUT]->(x)
RETURN count(*) AS created
"""

# 5. Evidence links: rows [{fid, uid, sha256, summary}]. The summary is kept on the edge (the
# finding's own wording) and copied onto Evidence once if it has none yet (G17).
CREATE_SUPPORTED_BY = """
UNWIND $rows AS r
MATCH (f:Finding {id: r.fid}), (e:Evidence {uid: r.uid})
SET e.summary = coalesce(e.summary, r.summary)
CREATE (f)-[:SUPPORTED_BY {sha256: r.sha256, summary: r.summary}]->(e)
RETURN count(*) AS created
"""

# 6a. LINKED_TO assertions (SIGINT): rows [{fid, aid, s, o, link_type, valid_from, valid_until}].
CREATE_LINKED_TO = """
UNWIND $rows AS r
MATCH (f:Finding {id: r.fid}), (s:Entity {uid: r.s}), (o:Entity {uid: r.o})
CREATE (f)-[:ASSERTS]->(a:Assertion {
  id: r.aid, investigation_id: $inv, type: 'LINKED_TO', link_type: r.link_type,
  valid_from: r.valid_from, valid_until: r.valid_until
})
CREATE (a)-[:SUBJECT]->(s)
CREATE (a)-[:OBJECT]->(o)
RETURN count(*) AS created
"""

# 6b. PLACED_AT assertions (GEOINT, S3): rows [{fid, aid, s, l, valid_from, valid_until}].
CREATE_PLACED_AT = """
UNWIND $rows AS r
MATCH (f:Finding {id: r.fid}), (s:Entity {uid: r.s}), (l:Location {uid: r.l})
CREATE (f)-[:ASSERTS]->(a:Assertion {
  id: r.aid, investigation_id: $inv, type: 'PLACED_AT',
  valid_from: r.valid_from, valid_until: r.valid_until
})
CREATE (a)-[:SUBJECT]->(s)
CREATE (a)-[:AT]->(l)
RETURN count(*) AS created
"""
