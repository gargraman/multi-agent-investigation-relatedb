"""``POST /v1/search`` Cypher (S4a FR-208): structured filter over committed findings.

Filters are AND between lists and OR inside a list; an empty list is "no filter". The caller's
clearance and trust predicates (``queries.context``) always apply, so for the analysis-agent the
requested ``statuses`` is effectively intersected with the trusted set. The row shape is exactly
``queries.context.FINDINGS``: both reads are assembled by one helper in the repository.
"""

from nexus_context_service.queries.context import (
    FINDING_COLUMNS,
    RANK_HIDDEN,
    RANK_VISIBLE,
    TRUSTED,
)

# The request filters other than clearance: shared by the result query and the hidden-count query
# so "hidden above clearance" counts only findings that would otherwise have matched.
_MATCHING = """(size($kinds) = 0 OR f.kind IN $kinds)
  AND (size($statuses) = 0 OR f.status IN $statuses)
  AND (size($entity_ids) = 0
       OR EXISTS { MATCH (f)-[:ABOUT]->(e:Entity) WHERE e.manifest_id IN $entity_ids })"""

# One row more than the limit is fetched: ``truncated`` is "more findings matched than limit".
SEARCH_FINDINGS = f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)
WHERE {RANK_VISIBLE} AND {TRUSTED}
  AND {_MATCHING}
{FINDING_COLUMNS}
ORDER BY f.created_at, f.id
LIMIT $fetch_limit
"""

# Above-clearance findings matching the other filters. Untrusted ones are never counted for the
# analysis-agent (TRUSTED first, as in ``COUNT_FILTERED``).
COUNT_FILTERED_MATCHING = f"""
MATCH (:Investigation {{id: $inv}})-[:CONTAINS]->(f:Finding)
WHERE {RANK_HIDDEN} AND {TRUSTED}
  AND {_MATCHING}
RETURN count(f) AS filtered
"""
