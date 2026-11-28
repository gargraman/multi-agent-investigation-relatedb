"""Read-audit Cypher (S4a FR-204/FR-205, NFR-202).

A read audit event records the **count** of records hidden by clearance, never their ids; the
only ids it links to are the findings actually returned (``RETURNED`` receipt edges, analysis
reads only).
"""

CREATE_READ_AUDIT_EVENT = """
CREATE (a:AuditEvent {
  id: $audit_id, caller: $caller, actor: $actor, operation: $operation,
  target_id: $target_id, authz: 'ALLOWED', outcome: $outcome, at: $now, trace_id: $trace_id,
  filtered_above_clearance: $filtered, task_id: $task_id
})
WITH a
OPTIONAL MATCH (t:Investigation {id: $target_id})
FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | CREATE (a)-[:CONCERNS]->(t))
"""

CREATE_RETURNED = """
MATCH (a:AuditEvent {id: $audit_id})
UNWIND $finding_ids AS fid
MATCH (f:Finding {id: fid})
CREATE (a)-[:RETURNED]->(f)
RETURN count(*) AS created
"""
