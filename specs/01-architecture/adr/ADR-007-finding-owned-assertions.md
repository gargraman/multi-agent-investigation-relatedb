# ADR-007: Agent facts in the graph — finding-owned Assertion nodes

- **Status:** Proposed
- **Date:** 2026-11-24
- **Spec refs:** §6.3, §9.1, §9.3, §10 (I2, I4)

## Context
v1.1 modelled agent observations as edges directly between entities: `(Entity)-[:OBSERVED_AT {from,to}]->(Location)` and `(Entity)-[:RELATED_TO]->(Entity)`. That caused two problems:

1. **No writer.** `POST /v1/findings` carried only `ABOUT` edges, and the Finding schema had no structured location or time fields. So nothing could create the edges that the hidden-link query (I4) needs.
2. **Facts outside provenance.** An entity-level edge isn't tied to the finding that claimed it. Retracting evidence (I2) flags findings as `NEEDS_REVIEW`, but the `OBSERVED_AT` edge stays. I4 would keep showing a hidden link built on retracted evidence, which contradicts I2 in the middle of the demo.

The plain version: in this system, *who said it* matters as much as *what was said*. A fact with no author is unauditable.

## Decision
- Agents submit structured `assertions[]` inside each finding. P0 has two types:
  - `PLACED_AT {subject_entity_id, location_id, valid_from, valid_until}`;
  - `LINKED_TO {subject_entity_id, object_entity_id, link_type, valid_from, valid_until}`.
- The Context Service stores each one as an `Assertion` node, written in the same transaction as its finding:
  ```text
  (Finding)-[:ASSERTS]->(Assertion)-[:SUBJECT]->(Entity)
                         (Assertion)-[:OBJECT]->(Entity)          # LINKED_TO
                         (Assertion)-[:AT]->(Entity:Location)     # PLACED_AT
  ```
- An assertion has **no status of its own**. A query trusts it only while its owning finding is `ACTIVE` or `DISPUTED`. Retraction therefore removes dependent facts from I4 without extra writes.
- Entity↔entity edges (`RELATED_TO`) come **only from the scenario manifest** and are registered by the orchestrator. Agents never write them.
- Locations are manifest-defined grid cells (`Entity:Location`) with a **cartesian** `position` point. The GEOINT detector snaps each observation to its cell deterministically. The LLM chooses among detector candidates and never makes up coordinates or windows.
- The Context Service validates each assertion: referenced entities and locations exist in the investigation, `valid_from ≤ valid_until`, and the assertion type is allowed for the submitting agent (SIGINT: `LINKED_TO`; GEOINT: `PLACED_AT`).

### I4 query sketch (to be confirmed against Relate in the S0 spike)
```cypher
MATCH (inv:Investigation {id: $inv})
MATCH (inv)-[:CONTAINS]->(fs:Finding)-[:ASSERTS]->(:Assertion {type:'LINKED_TO'})-[:SUBJECT]->(a:Entity)
MATCH (inv)-[:CONTAINS]->(fa:Finding)-[:ASSERTS]->(pa:Assertion {type:'PLACED_AT'})-[:SUBJECT]->(a),
      (pa)-[:AT]->(la:Location)
MATCH (inv)-[:CONTAINS]->(fb:Finding)-[:ASSERTS]->(pb:Assertion {type:'PLACED_AT'})-[:SUBJECT]->(b:Entity),
      (pb)-[:AT]->(lb:Location)
WHERE b <> a
  AND fs.source_agent_id <> fa.source_agent_id
  AND all(f IN [fs, fa, fb] WHERE f.status IN ['ACTIVE', 'DISPUTED'])
  AND point.distance(la.position, lb.position) < $radius
  AND pa.valid_from < pb.valid_until AND pb.valid_from < pa.valid_until
  AND NOT EXISTS { (a)<-[:SUBJECT]-(:Assertion {type:'LINKED_TO'})-[:OBJECT]->(b) }
RETURN DISTINCT a.id AS entity_a, b.id AS entity_b,
       collect(DISTINCT fs.id) + collect(DISTINCT fa.id) + collect(DISTINCT fb.id) AS via_findings
```
The query runs with the caller's clearance filter applied to every finding it matches.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Assertion node owned by Finding (chosen)** | Every fact has an author; retraction propagates for free; one generic shape for both assertion types; I4 stays a pure graph traversal | One extra hop per fact; more nodes (trivial at demo scale) |
| Edge from Finding with an `entity_id` property, e.g. `(Finding)-[:PLACES {entity_id, from, to}]->(Location)` | Fewer nodes | Joins on a property instead of traversing; weaker graph story; `LINKED_TO` needs a different shape |
| Entity-level edges carrying `finding_id` (v1.1 shape + property) | Short I4 query | Retraction needs extra writes or property filters; easy to forget the filter; v1.1's original bug comes back |

## Consequences
- (+) I2 and I4 stay consistent by construction. Provenance (I1) can extend down to the individual fact.
- (+) Adding assertion types later (e.g. `MEMBER_OF`) is a schema change, not a remodel.
- (−) FindingDraft and the batch contract get more complex. Detectors must output structured candidates, which they need anyway.
- (−) The I4 query is longer. It must be verified against Relate's `EXISTS {}` subquery and cartesian `point.distance` support in the S0 spike.
