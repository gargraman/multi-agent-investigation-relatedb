"""``POST /v1/assessments`` Cypher (S4a FR-209..212, FR-211a).

All statements run inside **one** lane transaction, in the order the repository documents:
idempotency -> fence -> receipt -> writes. A citation miss raises inside the transaction (rolling
everything back); the ``DENIED`` audit event is then written in its own lane run.

Storage shape (read back by ``queries.context.ASSESSMENT``): an ``Assessment`` node holding the
scalar fields (``summary``, ``model_id``, ``prompt_version``, ``schema_version``, ``task_id``,
``attempt``, ``status``, ``classification``, ``classification_rank``, ``created_at``) plus
``body_json``, the canonical JSON of ``conclusions``, ``contradictions``, ``information_gaps``
and ``follow_up_questions``; ``(Assessment)-[:BELONGS_TO]->(Investigation)`` and one
``(Assessment)-[:USES_FINDING]->(Finding)`` per used id.

Edges (FR-213): an accepted proposed edge is a ``(Finding)-[:CORROBORATES|CONTRADICTS|DERIVED_FROM
{assessment_id, created_at}]->(Finding)`` relationship. A relationship type cannot be a Cypher
parameter, so each type has its own constant, picked from the ``EDGE_*`` maps below by an
``EdgeType`` the contract has already validated: no request text reaches a query.
"""

# 1 + 2. Idempotency and fence in one read. The replay decision is taken before the fence (G15).
CHECK_SUBMISSION = """
MATCH (i:Investigation {id: $inv})
OPTIONAL MATCH (s:Submission {idempotency_key: $key})
RETURN i.write_state AS write_state, s.content_hash AS content_hash,
       s.response_json AS response_json, s.assessment_id AS assessment_id
"""

# 3. The task's retrieval receipt within this investigation (FR-206): every finding an
# analysis-agent read for this task returned, restricted to findings this investigation CONTAINS
# (a receipt from another investigation never counts, Review Focus 2). Task-scoped, not
# attempt-scoped: a retry may cite what an earlier attempt retrieved.
RECEIPT = """
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding)<-[:RETURNED]-(a:AuditEvent)
WHERE a.caller = $caller AND a.task_id = $task_id
RETURN DISTINCT f.id AS id, f.kind AS kind, f.status AS status,
       f.classification AS classification, f.classification_rank AS classification_rank
"""

CREATE_SUBMISSION = """
CREATE (:Submission {
  id: $submission_id, idempotency_key: $key, content_hash: $content_hash,
  investigation_id: $inv, task_id: $task_id, attempt: $attempt, caller: $caller,
  assessment_id: $assessment_id, response_json: $response_json, committed_at: $now
})
"""

CREATE_ASSESSMENT = """
MATCH (i:Investigation {id: $inv})
CREATE (a:Assessment {
  id: $assessment_id, investigation_id: $inv, submission_id: $submission_id,
  task_id: $task_id, attempt: $attempt, status: $status,
  classification: $classification, classification_rank: $classification_rank,
  summary: $summary, body_json: $body_json, model_id: $model_id,
  prompt_version: $prompt_version, schema_version: $schema_version, created_at: $now
})
CREATE (a)-[:BELONGS_TO]->(i)
RETURN count(a) AS created
"""

CREATE_USES_FINDING = """
MATCH (a:Assessment {id: $assessment_id})
UNWIND $finding_ids AS fid
MATCH (:Investigation {id: $inv})-[:CONTAINS]->(f:Finding {id: fid})
CREATE (a)-[:USES_FINDING]->(f)
RETURN count(*) AS created
"""


def _edge_queries() -> tuple[dict[str, str], dict[str, str]]:
    """``(present, create)`` statements per edge type. ``CORROBORATES`` / ``CONTRADICTS`` are
    compared undirected, ``DERIVED_FROM`` directed (FR-213)."""
    present: dict[str, str] = {}
    create: dict[str, str] = {}
    for rel, arrow in (
        ("CORROBORATES", "-"),
        ("CONTRADICTS", "-"),
        ("DERIVED_FROM", "->"),
    ):
        present[rel] = (
            "MATCH (s:Finding {id: $src}), (t:Finding {id: $tgt}) "
            f"RETURN EXISTS {{ (s)-[:{rel}]{arrow}(t) }} AS present"
        )
        create[rel] = (
            "MATCH (s:Finding {id: $src}), (t:Finding {id: $tgt}) "
            f"CREATE (s)-[:{rel} {{assessment_id: $assessment_id, created_at: $now}}]->(t) "
            "RETURN count(*) AS created"
        )
    return present, create


EDGE_PRESENT, EDGE_CREATE = _edge_queries()

# Adding ``source -> target`` closes a ``DERIVED_FROM`` cycle iff ``target`` already reaches
# ``source`` through ``DERIVED_FROM`` edges (including ones written earlier in this transaction).
DERIVED_FROM_CLOSES_CYCLE = """
MATCH (s:Finding {id: $src}), (t:Finding {id: $tgt})
RETURN EXISTS { (t)-[:DERIVED_FROM]->+(s) } AS cycle
"""

# FR-213: each accepted CONTRADICTS moves ACTIVE findings to DISPUTED, version + 1. Any other
# status (NEEDS_REVIEW, SUPERSEDED, already DISPUTED) fails the WHERE and is left untouched.
DISPUTE_FINDINGS = """
MATCH (f:Finding)
WHERE f.id IN $ids AND f.status = 'ACTIVE'
SET f.status = 'DISPUTED', f.version = f.version + 1
RETURN count(f) AS changed
"""
