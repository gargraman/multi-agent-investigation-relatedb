"""``POST /v1/investigations/{id}/evidence/{evidence_id}/retract`` Cypher (S4a FR-216, I2).

All statements run inside **one** lane transaction. ``IMPACT`` is the spike's I2
(``spikes/relate/queries.py``, quantified path ``<-[:DERIVED_FROM]-+`` kept) scoped to one
investigation: the evidence is addressed by its investigation-scoped ``uid``, so the same
manifest evidence id in another investigation is never touched (Review Focus 4).
"""

# 1. The evidence, scoped to the investigation, and the Decision that retracted it (if any).
# One row per investigation; ``e`` is null when the evidence is unknown there.
GET_EVIDENCE = """
MATCH (i:Investigation {id: $inv})
OPTIONAL MATCH (i)-[:HAS_EVIDENCE]->(e:Evidence {uid: $uid})
OPTIONAL MATCH (d:Decision)-[:DISPOSES]->(e)
RETURN e.status AS status, d.id AS decision_id, d.impact_json AS impact_json
"""

# 2. I2. Findings SUPPORTED_BY the evidence plus every finding reaching them through
# DERIVED_FROM at any depth, every node on the path in this investigation (the predicate sits
# inside the quantified path pattern, so a foreign finding cannot be traversed through).
# ``SUPERSEDED`` findings (and assessments) are neither changed nor listed. Assertions are those
# of the listed findings.
IMPACT = """
MATCH (:Investigation {id: $inv})-[:HAS_EVIDENCE]->(e:Evidence {uid: $uid})
MATCH (e)<-[:SUPPORTED_BY]-(f:Finding)
OPTIONAL MATCH (f) (()<-[:DERIVED_FROM]-(:Finding {investigation_id: $inv}))+ (g)
WITH collect(DISTINCT f) + collect(DISTINCT g) AS impacted
UNWIND impacted AS x
WITH DISTINCT x
WHERE x.status <> 'SUPERSEDED'
OPTIONAL MATCH (a:Assessment)-[:USES_FINDING]->(x)
WHERE a.status <> 'SUPERSEDED'
OPTIONAL MATCH (x)-[:ASSERTS]->(s:Assertion)
RETURN collect(DISTINCT x.id) AS findings, collect(DISTINCT a.id) AS assessments,
       collect(DISTINCT s.id) AS assertions
"""

RETRACT_EVIDENCE = """
MATCH (e:Evidence {uid: $uid})
SET e.status = 'RETRACTED', e.retracted_at = $now
"""

# A finding that changes status gets version + 1 (as DISPUTED does); one already NEEDS_REVIEW is
# unchanged (it stays in the impact set: it is affected, just not changed).
FLAG_FINDINGS = """
MATCH (f:Finding)
WHERE f.id IN $ids AND f.status <> 'NEEDS_REVIEW' AND f.status <> 'SUPERSEDED'
SET f.status = 'NEEDS_REVIEW', f.version = f.version + 1
RETURN count(f) AS changed
"""

FLAG_ASSESSMENTS = """
MATCH (a:Assessment)
WHERE a.id IN $ids AND a.status <> 'NEEDS_REVIEW' AND a.status <> 'SUPERSEDED'
SET a.status = 'NEEDS_REVIEW'
RETURN count(a) AS changed
"""

CREATE_DECISION = """
MATCH (e:Evidence {uid: $uid})
CREATE (d:Decision {
  id: $decision_id, investigation_id: $inv, actor: $actor, reason: $reason,
  created_at: $now, impact_json: $impact_json
})
CREATE (d)-[:DISPOSES]->(e)
RETURN count(d) AS created
"""
