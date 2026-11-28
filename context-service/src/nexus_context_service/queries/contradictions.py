"""``GET /v1/investigations/{id}/entities/{entity_id}/contradictions`` Cypher (S4a FR-217, I3).

``PAIRS`` is the spike's I3 (``spikes/relate/queries.py``: two findings ``ABOUT`` one entity that
are ``CONTRADICTS``-ed) scoped to one investigation (the entity is addressed by its
investigation-scoped ``uid``; both findings must be ``CONTAINS``-ed by it). ``CONTRADICTS`` is
undirected in meaning, so the match ignores direction and ``f1.id < f2.id`` yields each pair once,
lower id first. Both findings must be within the caller's clearance; a pair with a hidden member
is dropped.
"""

# 1. The entity, scoped to the investigation. No row: unknown investigation or entity.
ENTITY_EXISTS = """
MATCH (:Investigation {id: $inv})-[:HAS_ENTITY]->(e:Entity {uid: $uid})
RETURN count(e) AS n
"""

PAIRS = """
MATCH (i:Investigation {id: $inv})-[:HAS_ENTITY]->(x:Entity {uid: $uid})
MATCH (i)-[:CONTAINS]->(f1:Finding)-[:ABOUT]->(x)
MATCH (i)-[:CONTAINS]->(f2:Finding)-[:ABOUT]->(x)
WHERE f1.id < f2.id AND (f1)-[:CONTRADICTS]-(f2)
  AND f1.classification_rank <= $max_rank AND f2.classification_rank <= $max_rank
RETURN DISTINCT f1.id AS finding_a, f2.id AS finding_b
ORDER BY finding_a, finding_b
"""
