"""``GET /v1/assessments/{id}/provenance`` Cypher (S4a FR-215, I1).

``TRAIL`` is the spike's I1 (``spikes/relate/queries.py``: assessment ``USES_FINDING`` finding
``SUPPORTED_BY`` evidence) scoped to the assessment's own investigation: the findings must be
``CONTAINS``-ed by it and the evidence ``HAS_EVIDENCE``-ed by it. It is a trail, not a trusted
read: findings and evidence are returned whatever their status, so a retraction shows up as
``RETRACTED`` / ``NEEDS_REVIEW`` rather than as a gap. Only clearance applies.
"""

from nexus_context_service.queries.context import RANK_VISIBLE

# 1. The assessment and its investigation. No row: unknown. A row whose ``rank`` exceeds the
# caller's ``$max_rank``: hidden (the caller sees the same 404, the route audits the denial).
HEAD = """
MATCH (a:Assessment {id: $id})
OPTIONAL MATCH (a)-[:BELONGS_TO]->(i:Investigation)
RETURN a.classification_rank AS rank, i.id AS investigation_id
"""

# 2. I1. One row per (finding, evidence); a finding with no evidence yields one row with nulls.
TRAIL = f"""
MATCH (i:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)
MATCH (:Assessment {{id: $id}})-[:USES_FINDING]->(f)
WHERE {RANK_VISIBLE}
OPTIONAL MATCH (f)-[:SUPPORTED_BY]->(e:Evidence)<-[:HAS_EVIDENCE]-(i)
RETURN f.id AS finding_id, f.source_agent_id AS source_agent_id, f.status AS status,
       f.classification AS classification, e.manifest_id AS evidence_id, e.sha256 AS sha256,
       e.owner_node_id AS owner_node_id, e.status AS evidence_status
ORDER BY f.id, e.manifest_id
"""
