# ADR-001: Graph store — Relate embedded in the Context Service

- **Status:** Proposed (to be confirmed by the S0 spike)
- **Date:** 2026-11-24
- **Spec refs:** §4.3, §9, §10, §21

## Context
Nexus needs a shared knowledge graph for findings, evidence references, entities, relationships, assessments and audit. The baseline choice is **Relate**, Neo4j's open-source embedded graph database (Apache-2.0, Rust, Cypher 25), released on 11 Nov 2026.

Documented facts (vendor README/blog, Nov 2026; to be verified):
- in-process Python package `relate-db`;
- WAL + snapshot durability;
- indexes, constraints, point and vector indexes;
- **one graph per store, a single user/agent per process, working set in memory**;
- optional `relate serve` (Bolt/HTTP/MCP), whose Docker image is labelled dev/test.

## Decision
- The Context Service embeds Relate in-process. There is no separate Relate container.
- The Context Service runs as exactly one replica. All writes go through one serialized write lane (dedicated worker thread).
- Each domain operation is one Relate transaction, including its audit node.
- `relate serve` (Bolt/HTTP/MCP) is **never** enabled in Compose; it would expose raw Cypher and bypass governance.
- All access goes through a `GraphRepository` interface. Cypher lives in `context-service/queries/`.
- Pin the `relate-db` version; an upgrade requires re-running the spike suite.
- Back up with `relate clone`.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Relate embedded (chosen)** | Zero ops; matches the "single owner" rule exactly; Cypher; point index for GEOINT | Two weeks old; single process; no external PRs accepted |
| `relate serve` sidecar container | Separate process; Bolt drivers | Opens a raw-Cypher surface; dev/test image; extra container |
| Neo4j Community server | Mature; same Cypher | Heavier (JVM, memory); GPL licence considerations; extra container |
| Postgres (tables + edge table) | Mature; also covers operational state | Provenance/impact queries need recursive CTEs; weakens the graph-value story |

## Consequences
- (+) One fewer service; no network path to the store; governance is enforced by construction.
- (−) No horizontal scaling of the Context Service. This is accepted for the MVP.
- (−) Maturity risk, mitigated by the S0 spike, the version pin and the `GraphRepository` fallback to a Neo4j server over Bolt.

## S0 spike exit criteria
1. Constraint, index and point-index DDL works as the spec assumes.
2. Transaction API: multi-statement commit and rollback.
3. 50 concurrent HTTP requests through the write lane: no corruption, no deadlock.
4. `kill -9` during writes, then restart: every acknowledged transaction is present.
5. `relate clone` backup restores correctly.
6. Review `docs/limitations.md` against the queries for I1–I4 (quantified paths, `point.distance`).

If criteria 1–4 fail: switch to Neo4j Community through `GraphRepository` and update this ADR.
