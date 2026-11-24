# ADR-003: Task execution — async submit/poll with agent-persisted task state

- **Status:** Proposed
- **Date:** 2026-11-24
- **Spec refs:** §8.3–§8.6, AC12, AC13

## Context
An LLM-backed task takes 10–60 s, including retries. With no broker in P0, a synchronous HTTP request cannot survive an agent or orchestrator restart, so stop/restart recovery (AC12/13) would be impossible.

## Decision
- `POST /v1/tasks` returns `202` only after the agent has persisted the task. Task IDs are orchestrator-generated. Each request carries an `attempt`; the agent dedups on `(task_id, attempt)`, and a higher attempt cancels and supersedes any earlier attempt that is still running (v1.2, spec §8.3).
- The orchestrator polls `GET /v1/tasks/{id}` every 2 s, with jitter, and enforces deadlines (180 s specialist, 240 s analysis).
- On restart, an agent marks in-flight tasks `FAILED (agent_restarted)` unless they are safely resumable.
- The orchestrator re-dispatches a `TIMED_OUT` or `agent_restarted` task **once** (`attempt = 2`), if the agent is ready. Batch idempotency on `task_id` ensures at most one attempt commits findings. Otherwise the partial-completion policy applies.
- On orchestrator startup, a resume sweep reloads `RUNNING`/`ANALYZING` investigations and their tasks from SQLite and restarts the pollers (ADR-008).

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Polling (chosen)** | Stateless on the wire; survives restarts on both sides | Some latency and chatter (negligible at demo scale) |
| Callbacks (agent → orchestrator) | Lower latency | Needs callback retry and an outbox at the agent; harder restart semantics |
| NATS JetStream | Replay, buffering | P1 infrastructure; premature |

## Consequences
- (+) AC12/AC13 become testable with simple chaos tests.
- (−) Trace continuity relies on explicit `traceparent` propagation in the task body, verified by the Tempo test.
