"""``GET /v1/investigations/{id}/graph`` Cypher (S4a FR-219, I6; ADR-007 G11).

The operator's knowledge-graph view of one investigation: seven node labels, each with a fixed
property whitelist (every column below is named, so a property added to a stored node later is
never exported by accident; ``Assessment.body_json`` in particular stays in the store), and the
edges between the nodes returned. ``AuditEvent`` and ``Submission`` are never selected, so
neither they nor their ``CONCERNS`` / ``COMMITTED`` / ``RETURNED`` edges can appear.

Visibility: findings are filtered by the caller's rank and trust (``queries.context``
fragments), assessments by rank. Assertions and every edge are reached *through* a visible
finding / assessment, so a hidden node is dropped together with its assertions and edges. A
``Decision`` belongs to the investigation through the evidence it ``DISPOSES``. Every statement
is anchored on the investigation (``HAS_ENTITY`` / ``HAS_EVIDENCE`` / ``CONTAINS`` /
``BELONGS_TO``), so nodes of another investigation never appear.

Node queries return ``id`` plus the whitelist columns; edge queries return ``source``,
``target`` (node ids) and ``type``.
"""

from nexus_context_service.queries.context import RANK_HIDDEN, RANK_VISIBLE, TRUSTED

FINDING_VISIBLE = f"{RANK_VISIBLE} AND {TRUSTED}"
ASSESSMENT_VISIBLE = "a.classification_rank <= $max_rank"

# --- nodes (label -> statement) --------------------------------------------------------------

INVESTIGATION = """
MATCH (i:Investigation {id: $inv})
RETURN i.id AS id, i.status AS status, i.scenario_id AS scenario_id
"""

ENTITIES = """
MATCH (:Investigation {id: $inv})-[:HAS_ENTITY]->(e:Entity)
RETURN e.uid AS id, e.manifest_id AS manifest_id, e.type AS type, e.status AS status
ORDER BY e.uid
"""

EVIDENCE = """
MATCH (:Investigation {id: $inv})-[:HAS_EVIDENCE]->(e:Evidence)
RETURN e.uid AS id, e.manifest_id AS manifest_id, e.classification AS classification,
       e.status AS status
ORDER BY e.uid
"""

FINDINGS = f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)
WHERE {FINDING_VISIBLE}
RETURN f.id AS id, f.kind AS kind, f.status AS status, f.statement AS statement,
       f.confidence_band AS confidence_band, f.classification AS classification,
       f.source_agent_id AS source_agent_id
ORDER BY f.id
"""

ASSERTIONS = f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)-[:ASSERTS]->(s:Assertion)
WHERE {FINDING_VISIBLE}
RETURN s.id AS id, s.type AS type, s.valid_from AS valid_from, s.valid_until AS valid_until,
       s.link_type AS link_type
ORDER BY s.id
"""

ASSESSMENTS = f"""
MATCH (a:Assessment)-[:BELONGS_TO]->(:Investigation {{id: $inv}})
WHERE {ASSESSMENT_VISIBLE}
RETURN a.id AS id, a.status AS status, a.summary AS summary,
       a.classification AS classification
ORDER BY a.id
"""

# A Decision is part of the investigation when it disposes of one of its evidence items.
DECISIONS = """
MATCH (:Investigation {id: $inv})-[:HAS_EVIDENCE]->(:Evidence)<-[:DISPOSES]-(d:Decision)
RETURN DISTINCT d.id AS id, d.actor AS actor, d.created_at AS created_at
ORDER BY d.id
"""

NODE_QUERIES = {
    "Investigation": INVESTIGATION,
    "Entity": ENTITIES,
    "Evidence": EVIDENCE,
    "Finding": FINDINGS,
    "Assertion": ASSERTIONS,
    "Assessment": ASSESSMENTS,
    "Decision": DECISIONS,
}

# --- edges: every relationship between returned nodes ------------------------------------------

EDGE_QUERIES = (
    """
MATCH (i:Investigation {id: $inv})-[:HAS_ENTITY]->(e:Entity)
RETURN i.id AS source, e.uid AS target, 'HAS_ENTITY' AS type
""",
    """
MATCH (i:Investigation {id: $inv})-[:HAS_EVIDENCE]->(e:Evidence)
RETURN i.id AS source, e.uid AS target, 'HAS_EVIDENCE' AS type
""",
    f"""
MATCH (i:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)
WHERE {FINDING_VISIBLE}
RETURN i.id AS source, f.id AS target, 'CONTAINS' AS type
""",
    f"""
MATCH (a:Assessment)-[:BELONGS_TO]->(i:Investigation {{id: $inv}})
WHERE {ASSESSMENT_VISIBLE}
RETURN a.id AS source, i.id AS target, 'BELONGS_TO' AS type
""",
    f"""
MATCH (:Investigation {{id: $inv}})-[:HAS_ENTITY]->(e:Entity)<-[:ABOUT]-(f:Finding)
WHERE {FINDING_VISIBLE}
RETURN f.id AS source, e.uid AS target, 'ABOUT' AS type
""",
    f"""
MATCH (:Investigation {{id: $inv}})-[:HAS_EVIDENCE]->(e:Evidence)<-[:SUPPORTED_BY]-(f:Finding)
WHERE {FINDING_VISIBLE}
RETURN f.id AS source, e.uid AS target, 'SUPPORTED_BY' AS type
""",
    f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)-[:ASSERTS]->(s:Assertion)
WHERE {FINDING_VISIBLE}
RETURN f.id AS source, s.id AS target, 'ASSERTS' AS type
""",
    f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)-[:ASSERTS]->(s:Assertion)
WHERE {FINDING_VISIBLE}
MATCH (s)-[:SUBJECT]->(e:Entity)
RETURN s.id AS source, e.uid AS target, 'SUBJECT' AS type
""",
    f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)-[:ASSERTS]->(s:Assertion)
WHERE {FINDING_VISIBLE}
MATCH (s)-[:OBJECT]->(e:Entity)
RETURN s.id AS source, e.uid AS target, 'OBJECT' AS type
""",
    f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)-[:ASSERTS]->(s:Assertion)
WHERE {FINDING_VISIBLE}
MATCH (s)-[:AT]->(e:Entity)
RETURN s.id AS source, e.uid AS target, 'AT' AS type
""",
    f"""
MATCH (a:Assessment)-[:BELONGS_TO]->(:Investigation {{id: $inv}})
MATCH (a)-[:USES_FINDING]->(f:Finding)<-[:CONTAINS]-(:Investigation {{id: $inv}})
WHERE {ASSESSMENT_VISIBLE} AND {FINDING_VISIBLE}
RETURN a.id AS source, f.id AS target, 'USES_FINDING' AS type
""",
    # Finding -> finding edges (accepted assessment edges): both ends must be visible.
    *(
        f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)-[:{rel}]->(g:Finding)
WHERE {FINDING_VISIBLE}
  AND g.investigation_id = $inv AND g.classification_rank <= $max_rank
  AND (NOT $trusted_only OR g.status IN $trusted_statuses)
RETURN f.id AS source, g.id AS target, '{rel}' AS type
"""
        for rel in ("CORROBORATES", "CONTRADICTS", "DERIVED_FROM")
    ),
    """
MATCH (:Investigation {id: $inv})-[:HAS_ENTITY]->(s:Entity)-[:RELATED_TO]->(t:Entity)
RETURN s.uid AS source, t.uid AS target, 'RELATED_TO' AS type
""",
    """
MATCH (:Investigation {id: $inv})-[:HAS_EVIDENCE]->(e:Evidence)<-[:DISPOSES]-(d:Decision)
RETURN d.id AS source, e.uid AS target, 'DISPOSES' AS type
""",
)

# --- what the caller's clearance hid (audit count, never ids; NFR-202) ---------------------------

COUNT_HIDDEN_FINDINGS = f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)
WHERE {RANK_HIDDEN} AND {TRUSTED}
RETURN count(f) AS filtered
"""

COUNT_HIDDEN_ASSESSMENTS = """
MATCH (a:Assessment)-[:BELONGS_TO]->(:Investigation {id: $inv})
WHERE a.classification_rank > $max_rank
RETURN count(a) AS filtered
"""
