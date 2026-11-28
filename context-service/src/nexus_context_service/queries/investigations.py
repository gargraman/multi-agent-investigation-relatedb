"""Investigation lifecycle Cypher (spec §9.1/§9.3, ADR-002 A1/A2, ADR-007 G5)."""

GET_INVESTIGATION = """
MATCH (i:Investigation {id: $id})
RETURN i.content_hash AS content_hash, i.write_state AS write_state, i.status AS status,
       i.closed_at AS closed_at,
       COUNT { (i)-[:HAS_ENTITY]->(:Entity) } AS entity_count,
       COUNT { (i)-[:HAS_EVIDENCE]->(:Evidence) } AS evidence_count
"""

CREATE_INVESTIGATION = """
CREATE (:Investigation {
  id: $id, content_hash: $content_hash, scenario_id: $scenario_id,
  scenario_version: $scenario_version, objective: $objective, grid_pitch: $grid_pitch,
  created_at: $now, closed_at: null, status: $status, write_state: 'OPEN'
})
"""

# Manifest entities are RESOLVED by definition (spec §9.1).
CREATE_ENTITIES = """
MATCH (i:Investigation {id: $id})
UNWIND $entities AS e
CREATE (n:Entity {
  uid: $id + ':' + e.manifest_id, manifest_id: e.manifest_id, investigation_id: $id,
  type: e.type, display_name: e.display_name, aliases: e.aliases, status: 'RESOLVED'
})
CREATE (i)-[:HAS_ENTITY]->(n)
"""

# LOCATION entities: secondary label + cartesian point at the cell centre (srid 7203).
CREATE_LOCATIONS = """
MATCH (i:Investigation {id: $id})
UNWIND $locations AS e
CREATE (n:Entity:Location {
  uid: $id + ':' + e.manifest_id, manifest_id: e.manifest_id, investigation_id: $id,
  type: e.type, display_name: e.display_name, aliases: e.aliases, status: 'RESOLVED',
  position: CASE WHEN e.x IS NULL THEN null ELSE point({x: e.x, y: e.y}) END
})
CREATE (i)-[:HAS_ENTITY]->(n)
"""

CREATE_RELATIONSHIPS = """
UNWIND $relationships AS r
MATCH (s:Entity {uid: $id + ':' + r.source}), (t:Entity {uid: $id + ':' + r.target})
CREATE (s)-[:RELATED_TO {type: r.type}]->(t)
RETURN count(*) AS created
"""

CREATE_EVIDENCE = """
MATCH (i:Investigation {id: $id})
UNWIND $evidence AS ev
CREATE (n:Evidence {
  uid: $id + ':' + ev.manifest_id, manifest_id: ev.manifest_id, investigation_id: $id,
  owner_node_id: ev.owner_node, sha256: ev.sha256, classification: ev.classification,
  status: 'ACTIVE'
})
CREATE (i)-[:HAS_EVIDENCE]->(n)
"""

CLOSE_INVESTIGATION = """
MATCH (i:Investigation {id: $id})
SET i.write_state = 'CLOSED', i.status = $status, i.closed_at = $now
"""

# Append-only audit (spec §14.4). CONCERNS is linked only when the target node exists (a denial
# may name an id that was never created). Targets: an Investigation, a Submission for a
# committed findings batch (S2 G18), or a Decision for a retraction (S4a FR-216). Ids are
# prefix-distinct, so at most one of the three matches.
CREATE_AUDIT_EVENT = """
CREATE (a:AuditEvent {
  id: $audit_id, caller: $caller, actor: $actor, operation: $operation,
  target_id: $target_id, authz: $authz, outcome: $outcome, at: $now, trace_id: $trace_id
})
WITH a
OPTIONAL MATCH (t:Investigation {id: $target_id})
FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | CREATE (a)-[:CONCERNS]->(t))
WITH a
OPTIONAL MATCH (s:Submission {id: $target_id})
FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END | CREATE (a)-[:CONCERNS]->(s))
WITH a
OPTIONAL MATCH (d:Decision {id: $target_id})
FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [1] END | CREATE (a)-[:CONCERNS]->(d))
"""
