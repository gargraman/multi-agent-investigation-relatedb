"""``GET /v1/investigations/{id}/context`` read model Cypher (S2 spec FR-044, G5; S4a FR-202).

Flat row queries assembled in Python (one read transaction on the lane), rather than nested
collect subqueries: simpler to reason about on Relate and each query stays a plain pattern.

The row queries other than ``FINDINGS`` are restricted to ``$finding_ids`` (the findings being
assembled), so a search assembles only what it returns.

Every query is filtered by the caller's read scope: ``$max_rank`` (clearance) and
``$trusted_only`` over ``$trusted_statuses`` (analysis reads see ACTIVE / DISPUTED findings only).
The visibility predicate sits on the Finding in each, so a hidden finding's edges are hidden with
it. The predicate is built from two fragments, rank and trust, so a count query can reuse the
trust half without re-typing it; ``TRUSTED_STATUSES`` is the one place the trusted set is named.
"""

TRUSTED_STATUSES = ("ACTIVE", "DISPUTED")  # bound to ``$trusted_statuses`` by ``ReadScope``

RANK_VISIBLE = "f.classification_rank <= $max_rank"
RANK_HIDDEN = "f.classification_rank > $max_rank"
TRUSTED = "(NOT $trusted_only OR f.status IN $trusted_statuses)"
_VISIBLE = f"{RANK_VISIBLE} AND {TRUSTED}"


def _scoped(cypher: str) -> str:
    return cypher.replace("<VISIBLE>", _VISIBLE)


# Row shape of a finding, shared with ``queries.search`` so both reads assemble identically.
FINDING_COLUMNS = """RETURN f.id AS id, f.kind AS kind, f.status AS status,
       f.statement AS statement, f.confidence AS confidence, f.confidence_band AS confidence_band,
       f.classification AS classification, f.source_node_id AS source_node_id,
       f.task_id AS task_id, f.attempt AS attempt, f.model_id AS model_id,
       f.prompt_version AS prompt_version, f.valid_from AS valid_from,
       f.valid_until AS valid_until, f.created_at AS created_at"""

FINDINGS = _scoped(f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)
WHERE <VISIBLE>
{FINDING_COLUMNS}
ORDER BY f.created_at, f.id
""")

ABOUT = _scoped("""
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)-[:ABOUT]->(e:Entity)
WHERE <VISIBLE> AND f.id IN $finding_ids
RETURN f.id AS fid, e.manifest_id AS manifest_id
""")

EVIDENCE = _scoped("""
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)-[r:SUPPORTED_BY]->(e:Evidence)
WHERE <VISIBLE> AND f.id IN $finding_ids
RETURN f.id AS fid, e.manifest_id AS evidence_id, r.sha256 AS sha256, r.summary AS summary
""")

LINKED_TO = _scoped("""
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)-[:ASSERTS]->(a:Assertion)
WHERE a.type = 'LINKED_TO' AND <VISIBLE> AND f.id IN $finding_ids
MATCH (a)-[:SUBJECT]->(s:Entity), (a)-[:OBJECT]->(o:Entity)
RETURN f.id AS fid, s.manifest_id AS subject, o.manifest_id AS object,
       a.link_type AS link_type, a.valid_from AS valid_from, a.valid_until AS valid_until
""")

PLACED_AT = _scoped("""
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)-[:ASSERTS]->(a:Assertion)
WHERE a.type = 'PLACED_AT' AND <VISIBLE> AND f.id IN $finding_ids
MATCH (a)-[:SUBJECT]->(s:Entity), (a)-[:AT]->(l:Location)
RETURN f.id AS fid, s.manifest_id AS subject, l.manifest_id AS location,
       a.valid_from AS valid_from, a.valid_until AS valid_until
""")

# Findings the caller's clearance hides. The trusted filter applies first, so an untrusted
# finding is never counted (S4a FR-205): it is "not visible", not "above clearance".
COUNT_FILTERED = f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)
WHERE {RANK_HIDDEN} AND {TRUSTED}
RETURN count(f) AS filtered
"""

# Latest non-SUPERSEDED assessment the caller may read (Assessment.classification_rank, FR-212).
# Assessments are written by S4a Task 5, which wires this into the read model; until then
# ``InvestigationContext.assessment`` is always null and this query is not executed.
ASSESSMENT = """
MATCH (a:Assessment)-[:BELONGS_TO]->(:Investigation {id: $inv})
WHERE a.status <> 'SUPERSEDED' AND a.classification_rank <= $max_rank
RETURN a.id AS id
ORDER BY a.created_at DESC, a.id DESC
LIMIT 1
"""
