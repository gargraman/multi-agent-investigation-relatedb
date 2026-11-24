# ADR-002: Operational state — SQLite per service; audit + idempotency in Relate

- **Status:** Accepted (2026-11-24, with amendments A1–A5)
- **Date:** 2026-11-24
- **Spec refs:** §8.6, §8.7, §12.3, §14.4

## Context
v1.0 gave investigations, tasks, timeline events, checkpoints, idempotency keys and audit no defined store. Investigation status had three would-be owners. Restart recovery (AC13) needs durable state.

## Decision
- **Orchestrator:** SQLite on its own volume holds `investigations`, `tasks` and `timeline_events` (schema in spec §8.7; no LangGraph checkpointer, see ADR-008). `timeline_events.seq` is the monotonic sequence used for SSE `Last-Event-ID`. The orchestrator is the **sole owner of lifecycle status**.
- **Agents:** each has its own SQLite volume with a `tasks` table (state, attempt, result refs).
- **Context Service:** audit events and idempotency keys live **in Relate**, written in the same transaction as the operation they record. Idempotency keys sit on `Submission` nodes, one per task batch (spec §8.5). Denials are written as standalone audit transactions. Clearance-filtered reads write a `FILTERED` audit event with counts only (spec §9.5).

### Amendments accepted at sign-off
The orchestrator SQLite and Relate are **two stores with no shared transaction**. These rules cover the gap between them.

- **A1: `CREATED` investigations are resumable; investigation create is idempotent.**
  - The resume sweep covers `CREATED`, `RUNNING` and `ANALYZING`.
  - `POST /v1/investigations` on the Context Service is idempotent on the orchestrator-generated `investigation_id`: same ID and same content hash → `200` with the existing node; same ID and different content → `409`.
- **A2: Close reconciliation.**
  - `investigations.context_closed` (boolean) records whether the Context Service acknowledged `POST /v1/investigations/{id}/close`.
  - For every terminal investigation (`COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLED`) where `context_closed = false`, the resume sweep and a periodic reconciler (every 30 s) retry `close`. This is a broker-free outbox: the intent is persisted first, then retried until acknowledged.
  - `close` is idempotent.
- **A3: SQLite concurrency settings** (orchestrator and agents):
  - `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`;
  - exactly **one write connection** per process, serialized behind an asyncio lock; reads use separate connections;
  - every state change is one short transaction. No network call ever happens inside an open SQLite transaction.
- **A4: Audit-on-read stays cheap.** A `FILTERED` audit event is written only when `filtered_above_clearance > 0`. The operator (orchestrator, DEMO-HIGH) never triggers filtering, so UI polling of `/graph` or `/context` causes no audit writes and no write-lane load.
- **A5: Audit volume is a known limit.** Audit nodes count towards Relate's in-memory working set. This is negligible at demo scale; `make reset` clears it. Long-running deployments need audit export or a separate store (P1).

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **SQLite + audit in Relate (chosen)** | No extra container; no dual-write for findings; trivially atomic | Audit is co-located with the data it audits; SQLite is single-node; orchestrator ↔ Context Service needs the A1/A2 reconciliation |
| One Postgres for everything | Separate audit store; familiar | Extra container; graph ↔ Postgres dual-write needs an outbox/reconciliation (more than A1 + A2) |

## Consequences
- (+) Removes a whole class of consistency bugs (graph write committed, audit lost).
- (+) A1/A2 make the orchestrator ↔ Context Service hand-off crash-safe without a broker.
- (−) **Documented deviation from v1.0 §12.7:** audit is not in a separate protected store. It is append-only by API construction (no update or delete endpoints). Production needs a separate tamper-evident store (P1).
- (−) SQLite rules out multiple orchestrator replicas. That is acceptable for the MVP.

## Tests implied
- Unit: the resume sweep selects `CREATED`/`RUNNING`/`ANALYZING`; the reconciler selects terminal investigations with `context_closed = false`.
- Integration: duplicate `POST /v1/investigations` (same → 200, different → 409); `close` is idempotent.
- Chaos (S3): kill the orchestrator between the SQLite `CREATED` write and the Context Service create → the investigation proceeds after restart. Stop the Context Service during cancel → `close` eventually lands, and a late finding batch gets `409 investigation_cancelled`.
