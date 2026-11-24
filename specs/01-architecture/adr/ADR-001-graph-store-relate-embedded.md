# ADR-001: Graph store — Relate embedded in the Context Service

- **Status:** Accepted (2026-11-24, after the S0 spike; amendments R1–R6)
- **Date:** 2026-11-24
- **Spec refs:** §4.3, §9, §10, §21
- **Evidence:** [S0 spike report](../spike-report-relate.md); code in `spikes/relate/` (46 tests)

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
- The Context Service runs as exactly one replica. **All reads and writes** go through one serialized lane (dedicated worker thread owning the only connection; R1).
- Each domain operation is one Relate transaction, including its audit node.
- `relate serve` (Bolt/HTTP/MCP) is **never** enabled in Compose; it would expose raw Cypher and bypass governance.
- All access goes through a `GraphRepository` interface. Cypher lives in `context-service/queries/`.
- Pin the `relate-db` version **and the `relate` CLI version together** (R3); an upgrade requires re-running the spike suite.
- Back up with `relate clone` (standalone CLI binary, shipped in the Context Service image).

### Amendments from the S0 spike
The spike (macOS arm64, `relate-db==1.2.0`) passed criteria 2–7 and passed criterion 1 partially. I4 ran verbatim and returned exactly the planted `{A, C, B}`; `kill -9` over 20 rounds lost no acknowledged commit. Its findings change the rules as follows.

| # | Amendment |
|---|---|
| R1 | **One lane for reads and writes.** "Single user/agent per process" means in practice: a connection works only on the thread that opened it; only one writable handle per store, even within one process; read-only handles are a frozen snapshot from when they open and never see later commits. So the lane thread owns the only connection and serves **all** reads and writes. At demo scale this is cheap (50 writes + 20 reads in 0.12 s); it is the throughput ceiling, by design. |
| R2 | **Lane hygiene.** Transactions use the DB-API style (`conn.autocommit = False`, `commit()` / `rollback()`; no context manager). The lane rolls back on any exception (after a failed statement Relate refuses `commit()` until rollback), calls `flush()` periodically to checkpoint, and closes the store on graceful shutdown. |
| R3 | **Pin package and CLI together.** `relate clone` exists only in the standalone CLI binary, not in `relate-db`. The image installs the CLI of the same version, verified by the release's sha256. Five releases shipped in two weeks; pins move only with a spike re-run. |
| R4 | **Telemetry off, durability locked.** The CLI sends telemetry by default: set `DO_NOT_TRACK=1` and `RELATE_TELEMETRY=0` in every image and Makefile target. The Context Service refuses to start if `RELATE_DURABILITY_MODE` is set to anything other than `full` (the default); `everysecond`/`snapshot` silently accept data loss. |
| R5 | **Linux gate.** The spike ran on macOS only. Re-run the suite inside the Linux Context Service image, on the Compose named volume, before S1 closes. A failure there reopens this ADR. |
| R6 | **No point-index claims.** `CREATE POINT INDEX` succeeds but the planner never uses it (`PROFILE` shows a label scan). I4 reaches locations by traversal, so spatial matching is a post-traversal filter. Fine at demo scale; nothing may claim indexed spatial performance. |

Not tested: power loss (only process crash), Linux/Docker (R5), memory beyond the small spike graphs.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Relate embedded (chosen)** | Zero ops; matches the "single owner" rule exactly; Cypher 25 (I1–I4 run unmodified) | Alpha, two weeks old, fast release churn; one thread per store (R1); point index unused (R6); no external PRs accepted |
| `relate serve` sidecar container | Separate process; Bolt drivers | Opens a raw-Cypher surface; dev/test image; extra container |
| Neo4j Community server | Mature; same Cypher | Heavier (JVM, memory); GPL licence considerations; extra container |
| Postgres (tables + edge table) | Mature; also covers operational state | Provenance/impact queries need recursive CTEs; weakens the graph-value story |

## Consequences
- (+) One fewer service; no network path to the store; governance is enforced by construction.
- (−) No horizontal scaling of the Context Service, and no concurrent reads within it (R1). Accepted for the MVP.
- (−) Maturity risk, mitigated by the S0 spike, the version pin and the `GraphRepository` fallback to a Neo4j server over Bolt.

## S0 spike exit criteria (results: [spike report](../spike-report-relate.md))
1. Constraint, index and point-index DDL works as the spec assumes.
2. Transaction API: multi-statement commit and rollback.
3. 50 concurrent HTTP requests through the write lane: no corruption, no deadlock.
4. `kill -9` during writes, then restart: every acknowledged transaction is present.
5. `relate clone` backup restores correctly.
6. Review `docs/limitations.md` against the queries for I1–I4, and **run** the ADR-007 I4 sketch on seeded data: quantified paths, `EXISTS {}` subqueries with `MATCH … WHERE`, `[:A|B]` type alternation, cartesian `point.distance` with a point index, `datetime` comparison, `reduce`, map literals in `collect`.
7. Composite-free scoping works: a unique constraint on `uid` for Entity/Evidence, and two investigations of the same scenario coexist.

If criteria 1–4 fail: switch to Neo4j Community through `GraphRepository` and update this ADR.
