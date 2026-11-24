# ADR-002: Operational state — SQLite per service; audit + idempotency in Relate

- **Status:** Proposed
- **Date:** 2026-11-24
- **Spec refs:** §8, §14.4

## Context
v1.0 gave investigations, tasks, timeline events, checkpoints, idempotency keys and audit no defined store. Investigation status had three would-be owners. Restart recovery (AC13) needs durable checkpoints.

## Decision
- **Orchestrator:** SQLite on its own volume holds `investigations`, `tasks` and `timeline_events` (schema in spec §8.7; no LangGraph checkpointer, see ADR-008) (monotonic sequence for SSE `Last-Event-ID`). The orchestrator is the **sole owner of lifecycle status**.
- **Agents:** each has its own SQLite volume with a `tasks` table (state, attempt, result refs).
- **Context Service:** audit events and idempotency keys live **in Relate**, written in the same transaction as the operation they record. Idempotency keys sit on `Submission` nodes, one per task batch (spec §8.5, v1.2). Denials are written as standalone audit transactions; clearance-filtered reads write a `FILTERED` audit event with counts only (spec §9.5, v1.2).

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **SQLite + audit in Relate (chosen)** | No extra container; no dual-write for findings; trivially atomic | Audit is co-located with the data it audits; SQLite is single-node |
| One Postgres for everything | Separate audit store; familiar | Extra container; graph ↔ Postgres dual-write needs an outbox/reconciliation |

## Consequences
- (+) Removes a whole class of consistency bugs (graph write committed, audit lost).
- (−) **Documented deviation from v1.0 §12.7:** audit is not in a separate protected store. It is append-only by API construction (no update or delete endpoints). Production needs a separate tamper-evident store (P1).
- (−) SQLite rules out multiple orchestrator replicas. That is acceptable for the MVP.
