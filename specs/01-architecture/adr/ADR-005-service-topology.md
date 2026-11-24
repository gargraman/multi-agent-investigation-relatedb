# ADR-005: Service topology — merged orchestrator (API + workflow)

- **Status:** Accepted (2026-11-24, with amendments D1–D5; D6 parked)
- **Date:** 2026-11-24
- **Spec refs:** §5, §6.2, §8.6, §12.1, §14.1, §14.4, §16.1

## Context
v1.0 had a separate API Gateway and Coordinator, plus a separate Relate container. For a local single-host demo, the gateway adds one hop, one container and one more trace segment without any separation benefit, because both components share the same trust level.

Since v1.2 the case for merging is stronger. ADR-002 and ADR-008 put investigation and task state in **one SQLite file**, and SQLite can't be shared safely across containers. Splitting API and workflow would now force Postgres or an internal API between them.

## Decision
- One `orchestrator` service holds the FastAPI frontend API, the SSE stream and the deterministic investigation workflow (ADR-008), including the pollers, the resume sweep and the close reconciler (ADR-002 A2).
- The Relate container is removed (ADR-001).
- The P0 topology is frontend, orchestrator, sigint-node, geoint-node, analysis-node and context-service, with LGTM as an overlay.

### Amendments accepted at sign-off
The merged process runs five concurrent concerns: the HTTP API, SSE streams, pollers, the resume sweep and the reconciler. These rules govern how they share it.

- **D1: Single worker, leader lock.** Exactly one uvicorn worker and one replica. Background tasks start in the FastAPI `lifespan`. At startup the process takes a leader lock in SQLite (a `leader` row claimed with `BEGIN IMMEDIATE`, plus a heartbeat); a second process fails fast. This guards against accidental `--workers N` or a second replica double-dispatching tasks.
- **D2: Non-blocking event loop.** No blocking I/O on the loop: SQLite through `aiosqlite` or the single writer thread (ADR-002 A3), HTTP through async `httpx`. One slow call must never freeze the API and every SSE stream.
- **D3: SSE after commit.** Timeline events go to an in-process pub/sub (an asyncio queue per subscriber) only after their SQLite transaction commits. On connect, replay from `timeline_events` where `seq > Last-Event-ID`, then switch to live events, dropping duplicates by `seq`. The UI never sees a state that wasn't persisted.
- **D4: Operator identity reaches the audit.** On operator-initiated calls (retract, later dispositions), the orchestrator sends `X-Nexus-Actor: operator:<user>`. The Context Service trusts the header **only with the orchestrator's token** and records it as `actor` on the `AuditEvent` and `Decision`; `caller` stays `orchestrator`.
- **D5: Graceful shutdown changes no state.** On SIGTERM: stop accepting requests, close SSE streams, cancel pollers, roll back open transactions. No investigation or task status is touched; the resume sweep (ADR-002 A1) continues on restart.
- **D6 (parked, Good-to-have):** enforce the `api/` ↔ `workflow/` boundary with an `import-linter` contract.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Merged (chosen)** | Fewer moving parts; simpler tracing; in-process SSE pub/sub with no broker; one SQLite owner | API and workflow scale together (irrelevant for the MVP); five concerns share one event loop (D1–D3 govern this) |
| Separate gateway + coordinator | Cleaner separation for future scale | Extra service and hop; forces Postgres or an internal API for shared state; no MVP benefit |

## Consequences
- (+) Lower setup and debugging cost; no broker needed for live UI updates.
- (+) Retraction decisions are attributable to the human operator, not just the service.
- (−) Single process means single point of failure for the control plane; acceptable, and covered by the resume sweep and chaos tests.
- (−) Splitting later requires extracting `apps/orchestrator/api/` and moving state to a shared store. Keep the module boundary clean (D6) so the code split stays mechanical.

## Tests implied
- Unit: leader-lock acquisition fails for a second instance; timeline publish happens only after commit (inject a commit failure → no event published).
- Integration: SSE reconnect with `Last-Event-ID` replays the missed events exactly once; a retract call carries `X-Nexus-Actor`, and the header is ignored when sent with an agent token.
- Chaos (S3): SIGTERM mid-run → no state changed → restart resumes.
