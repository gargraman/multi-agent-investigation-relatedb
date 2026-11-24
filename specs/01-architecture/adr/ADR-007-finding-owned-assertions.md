# ADR-007: Agent facts in the graph — finding-owned Assertion nodes

- **Status:** Accepted (2026-11-24, after independent review; amendments G1–G11)
- **Date:** 2026-11-24
- **Spec refs:** §6.3, §9.1, §9.3, §10 (I2, I3, I4), §11.3, §12.1, §12.3, §22

## Context
v1.1 modelled agent observations as edges directly between entities: `(Entity)-[:OBSERVED_AT {from,to}]->(Location)` and `(Entity)-[:RELATED_TO]->(Entity)`. That caused two problems:

1. **No writer.** `POST /v1/findings` carried only `ABOUT` edges, and the Finding schema had no structured location or time fields. So nothing could create the edges that the hidden-link query (I4) needs.
2. **Facts outside provenance.** An entity-level edge isn't tied to the finding that claimed it. Retracting evidence (I2) flags findings as `NEEDS_REVIEW`, but the `OBSERVED_AT` edge stays, so I4 would keep showing a link built on retracted evidence.

The plain version: in this system, *who said it* matters as much as *what was said*. A fact with no author is unauditable.

An independent review of the first draft of this ADR found that the I4 story and query didn't fit the spec's own data: SIGINT analyses **handles**, GEOINT places **people**, and the link rested on GEOINT alone. This version fixes the model, the story and the query.

## Decision

### 1. Facts hang off findings
- Agents submit structured `assertions[]` inside each finding:
  - `LINKED_TO {subject, object, link_type, valid_from, valid_until}`: **SIGINT only**, between two `HANDLE` entities;
  - `PLACED_AT {subject, location, valid_from, valid_until}`: **GEOINT only**, a `PERSON` or `ASSET` at a `LOCATION`.
- The Context Service stores each one as an `Assertion` node in the same transaction as its finding:
  ```text
  (Finding)-[:ASSERTS]->(Assertion)-[:SUBJECT]->(Entity)
                         (Assertion)-[:OBJECT]->(Entity)          # LINKED_TO
                         (Assertion)-[:AT]->(Entity:Location)     # PLACED_AT
  ```
- An assertion has **no status of its own**. A query trusts it only while its owning finding is `ACTIVE` or `DISPUTED` **and** within the caller's clearance. Retraction therefore removes dependent facts from I4 with no extra writes.
- Agents never write entity↔entity edges. `RELATED_TO` (e.g. `handle USED_BY person`) comes only from the scenario manifest and is registered by the orchestrator.

### 2. Amendments from the independent review
| # | Amendment |
|---|---|
| G1 | **Handles bridge to people only through the manifest.** I4 resolves each SIGINT handle to its person via `RELATED_TO {type:'USED_BY'}`. Spec §9.3 states which entity types each agent asserts about. |
| G2 | **I4 is a two-hop path across agents (story A):** A —SIGINT→ C —GEOINT→ B. One hop per agent, so neither agent alone has any A…B connection. The §10 story and the §22 demo script are rewritten to match. |
| G3 | **Contact is symmetric.** `LINKED_TO` (`CONTACT`) is stored canonically (lower `manifest_id` as subject) and matched undirected. |
| G4 | **"Hidden" means no trusted direct A–B link of any kind:** no manifest `RELATED_TO`, no trusted handle contact between A's and B's handles, and no trusted co-location of A and B. Each check applies the same investigation, status and clearance predicates, so a retracted or over-clearance direct link neither suppresses nor leaks. |
| G5 | **Entities and Evidence are per investigation:** `uid = {investigation_id}:{manifest_id}`, with `(Investigation)-[:HAS_ENTITY]->(Entity)` and `-[:HAS_EVIDENCE]->(Evidence)`. Re-running a scenario never collides, and I3 never mixes investigations. Entity- and evidence-addressed endpoints are nested under `/investigations/{id}/`. |
| G6 | **Co-location = same or neighbouring grid cell:** `point.distance(...) <= 1.5 × grid.pitch`. The grid (`origin, pitch, cols, rows`) is defined in the manifest. |
| G7 | **Time:** Cypher `datetime` (UTC), half-open windows, overlap = `a.from < b.until AND b.from < a.until`. An assertion's window must lie inside its finding's window. |
| G8 | Each I4 result carries a **`disputed`** flag (true if any finding on the path is `DISPUTED`), shown in the UI. |
| G9 | Results are **de-duplicated per (A, C, B)** and return the cells and the overlap window, so the UI can explain *where* and *when*. |
| G10 | **Validation:** entities and location in this investigation; entity types per the G1 rule; `AT` targets a `Location`; `subject ≠ object`; subject ∈ the finding's `ABOUT`; window valid and inside the finding's. Assertions are part of the batch `content_hash`. |
| G11 | **Provenance granularity is the finding,** not the individual fact: retracting one evidence item drops every assertion on its finding. Per-assertion evidence links are P1. |

### 3. I4 query sketch (syntax to be confirmed in the S0 spike)
```cypher
// $inv: investigation id   $max_rank: caller clearance (1..3)   $radius: 1.5 * grid.pitch
MATCH (inv:Investigation {id: $inv})

// Hop 1, SIGINT: A's handle in contact with C's handle (undirected)
MATCH (inv)-[:CONTAINS]->(fs:Finding)-[:ASSERTS]->(l:Assertion {type: 'LINKED_TO'}),
      (l)-[:SUBJECT|OBJECT]->(ha:Entity)-[:RELATED_TO {type: 'USED_BY'}]->(a:Entity),
      (l)-[:SUBJECT|OBJECT]->(hc:Entity)-[:RELATED_TO {type: 'USED_BY'}]->(c:Entity)
WHERE ha <> hc AND a <> c
  AND fs.status IN ['ACTIVE', 'DISPUTED'] AND fs.classification_rank <= $max_rank

// Hop 2, GEOINT: C and B in neighbouring cells, overlapping windows
MATCH (inv)-[:CONTAINS]->(fc:Finding)-[:ASSERTS]->(pc:Assertion {type: 'PLACED_AT'})-[:SUBJECT]->(c),
      (pc)-[:AT]->(lc:Location)
MATCH (inv)-[:CONTAINS]->(fb:Finding)-[:ASSERTS]->(pb:Assertion {type: 'PLACED_AT'})-[:SUBJECT]->(b:Entity),
      (pb)-[:AT]->(lb:Location)
WHERE b <> a AND b <> c
  AND fs.source_agent_id <> fc.source_agent_id
  AND all(f IN [fc, fb] WHERE f.status IN ['ACTIVE', 'DISPUTED'] AND f.classification_rank <= $max_rank)
  AND point.distance(lc.position, lb.position) <= $radius
  AND pc.valid_from < pb.valid_until AND pb.valid_from < pc.valid_until

// Hidden: no trusted direct A–B link of any kind (G4)
  AND NOT EXISTS { (a)-[:RELATED_TO]-(b) }
  AND NOT EXISTS {
    MATCH (inv)-[:CONTAINS]->(fx:Finding)-[:ASSERTS]->(x:Assertion {type: 'LINKED_TO'}),
          (x)-[:SUBJECT|OBJECT]->(:Entity)-[:RELATED_TO {type: 'USED_BY'}]->(a),
          (x)-[:SUBJECT|OBJECT]->(:Entity)-[:RELATED_TO {type: 'USED_BY'}]->(b)
    WHERE fx.status IN ['ACTIVE', 'DISPUTED'] AND fx.classification_rank <= $max_rank
  }
  AND NOT EXISTS {
    MATCH (inv)-[:CONTAINS]->(f1:Finding)-[:ASSERTS]->(p1:Assertion {type: 'PLACED_AT'})-[:SUBJECT]->(a),
          (p1)-[:AT]->(l1:Location),
          (inv)-[:CONTAINS]->(f2:Finding)-[:ASSERTS]->(p2:Assertion {type: 'PLACED_AT'})-[:SUBJECT]->(b),
          (p2)-[:AT]->(l2:Location)
    WHERE all(f IN [f1, f2] WHERE f.status IN ['ACTIVE', 'DISPUTED'] AND f.classification_rank <= $max_rank)
      AND point.distance(l1.position, l2.position) <= $radius
      AND p1.valid_from < p2.valid_until AND p2.valid_from < p1.valid_until
  }

WITH a, c, b, fs, fc, fb, lc, lb,
     CASE WHEN pc.valid_from > pb.valid_from THEN pc.valid_from ELSE pb.valid_from END AS overlap_from,
     CASE WHEN pc.valid_until < pb.valid_until THEN pc.valid_until ELSE pb.valid_until END AS overlap_until
WITH a, c, b,
     collect(DISTINCT fs.id) AS sigint_findings,
     collect(DISTINCT fc.id) + collect(DISTINCT fb.id) AS geo_raw,
     collect(DISTINCT {cell_c: lc.manifest_id, cell_b: lb.manifest_id,
                       from: overlap_from, until: overlap_until}) AS windows,
     max(CASE WHEN 'DISPUTED' IN [fs.status, fc.status, fb.status] THEN 1 ELSE 0 END) AS any_disputed
RETURN a.manifest_id AS entity_a, c.manifest_id AS via, b.manifest_id AS entity_b,
       sigint_findings,
       reduce(acc = [], x IN geo_raw | CASE WHEN x IN acc THEN acc ELSE acc + x END) AS geoint_findings,
       windows,
       any_disputed = 1 AS disputed
```
Spike checks for this query: `EXISTS {}` subqueries with `MATCH … WHERE`, label/type alternation `[:SUBJECT|OBJECT]`, cartesian `point.distance` with the point index, `datetime` comparison, `reduce`, and map literals in `collect`.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Assertion node owned by Finding (chosen)** | Every fact has an author; retraction propagates for free; one generic shape; I4 stays a graph traversal | One extra hop per fact; more nodes (trivial at demo scale) |
| Edge from Finding with an `entity_id` property | Fewer nodes | Joins on a property instead of traversing; weaker graph story |
| Entity-level edges carrying `finding_id` (v1.1 shape) | Short I4 query | Retraction needs extra writes or filters; brings back v1.1's bug |
| I4 story B: co-location corroborated by contact | Simpler query | Really a join, not a discovery; overlaps with corroboration |

## Consequences
- (+) I2 and I4 stay consistent by construction; I4 is a genuine cross-agent path, which is the strongest "why a graph" moment.
- (+) Per-investigation scoping makes re-runs and concurrent investigations safe.
- (−) The scenario pack must plant the full I4 pattern (spec §11.3 oracle), including the absence of any direct A–B link.
- (−) Interface changes: entity- and evidence-addressed endpoints move under `/investigations/{id}/`; agents must emit typed assertions.
- (−) The I4 query is long and depends on several Cypher 25 features; it's a top item for the S0 spike, with a fallback of splitting it into two queries joined in Python.

## Tests implied
- Integration (seeded `baseline-corroboration`): I4 returns exactly the oracle's `{a, via, b}`; retract the SIGINT evidence → the path disappears; add a trusted direct A–B contact → the path disappears; make that direct contact DEMO-HIGH and query at DEMO-MID → the path still appears and nothing about the hidden link leaks.
- Integration: run the same scenario twice → no constraint violation, and I3 results stay per investigation.
- Unit: assertion validator (each G10 rule); canonical contact ordering; window-inside-finding check; co-location at a diagonal neighbour → true, two cells apart → false.
