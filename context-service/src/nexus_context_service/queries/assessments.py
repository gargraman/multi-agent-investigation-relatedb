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
RETURN DISTINCT f.id AS id, f.status AS status, f.classification AS classification,
       f.classification_rank AS classification_rank
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
