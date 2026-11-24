# ADR-003: Task execution — async submit/poll with agent-persisted task state

- **Status:** Accepted (2026-11-24, with amendments B1–B7 and B9; B8 parked)
- **Date:** 2026-11-24
- **Spec refs:** §6.4, §8.1–§8.6, §12.3, §17.1, AC12, AC13

## Context
An LLM-backed task takes 10–60 s, including retries. With no broker in P0, a synchronous HTTP request cannot survive an agent or orchestrator restart, so stop/restart recovery (AC12/13) would be impossible.

## Decision
- `POST /v1/tasks` returns `202` only after the agent has persisted the task. Task IDs are orchestrator-generated. Each request carries an `attempt`; the agent dedups on `(task_id, attempt)`, and a higher attempt cancels and supersedes any earlier attempt that is still running (spec §8.3).
- The orchestrator polls `GET /v1/tasks/{id}` every 2 s, with jitter, and enforces deadlines (180 s specialist, 240 s analysis).
- The orchestrator re-dispatches a `TIMED_OUT`, `FAILED (agent_restarted)` or `FAILED (task_lost)` task **once** (`attempt = 2`), if the agent is ready. Otherwise the partial-completion policy applies.
- Every agent commit is idempotent on `task_id` (findings batch, and the assessment per B6), so at most one attempt's results are ever committed.
- On orchestrator startup, a resume sweep reloads `CREATED`/`RUNNING`/`ANALYZING` investigations and their tasks from SQLite and restarts the pollers (ADR-002 A1, ADR-008).

### Amendments accepted at sign-off
- **B1: Fence-first cancel.** Cancel order is: (1) mark the investigation `CANCELLED` in SQLite; (2) `close` on the Context Service, the write fence; (3) cancel agent tasks, whether or not (2) succeeded; (4) mark the tasks `CANCELLED`. Terminal task states are **sticky**: a late `COMPLETED` poll result never overrides `CANCELLED`. Anything committed before the fence stays, shown as belonging to a cancelled investigation. Known window: if the Context Service is down at (2), the reconciler (ADR-002 A2) closes it within 30 s.
- **B2: Lost tasks.** A poll that returns `404` for a task the orchestrator believes is `DISPATCHED`/`RUNNING` means `FAILED (task_lost)`, which follows the re-dispatch rule.
- **B3: The agent enforces the deadline too.** The agent self-cancels at `deadline + 5 s`, with no commit, and marks the attempt `FAILED (deadline_exceeded)`. The orchestrator stays authoritative; the agent-side deadline stops token spend and narrows the window for an attempt race.
- **B4: Fresh deadline per attempt.** A re-dispatched attempt gets `min(now + task budget, investigation deadline)`.
- **B5: Nothing is resumable in P0.** On restart, the agent marks **every** `RUNNING` task `FAILED (agent_restarted)`.
- **B6: One idempotent assessment commit.** `POST /v1/assessments` carries the assessment, `USES_FINDING`, the proposed `CORROBORATES`/`CONTRADICTS`/`DERIVED_FROM` edges and the contradiction status changes, written in **one transaction** and idempotent on `task_id` (a `Submission` node, as for findings). Invalid edges are rejected individually and reported; they don't fail the whole assessment. **`/v1/relationships` is removed from P0.**
- **B7: Poll-call semantics.** Connection error, timeout or `5xx` on a poll is **not** a failure; polling continues until the deadline. Timeouts: 2 s connect, 5 s read.
- **B9:** the resume sweep text now matches ADR-002 A1.
- **B8 (parked, Good-to-have):** before calling the LLM on a re-dispatch, the agent checks whether a submission already exists for the `task_id` and short-circuits. Revisit if the S3 chaos tests show the duplicate LLM run matters.

### Cooperative cancellation inside an agent
An agent checks for cancellation before each LLM call and before the commit, and cancels an in-flight LLM call through asyncio. A commit that has already started is not interrupted; the Context Service fence decides whether it lands.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Polling (chosen)** | Stateless on the wire; survives restarts on both sides | Some latency and chatter (negligible: at most ~6 tasks at `MAX_CONCURRENT_INVESTIGATIONS=2`) |
| Callbacks (agent → orchestrator) | Lower latency | Needs callback retry and an outbox at every agent; harder restart semantics |
| NATS JetStream | Replay, buffering | P1 infrastructure; premature |

## Consequences
- (+) AC12/AC13 become testable with simple chaos tests.
- (+) Every write an agent makes is idempotent per task, so retries, re-dispatches and lost acknowledgements cannot duplicate or mix results.
- (−) Trace continuity relies on explicit `traceparent` propagation in the task body, verified by the Tempo test.
- (−) Interface change: `/v1/relationships` is gone; edge proposals travel inside the assessment.

## Tests implied
- Unit: the transition table rejects changes to sticky terminal states; the attempt deadline calculation; poll-outcome mapping (5xx → keep polling, 404 → `task_lost`).
- Integration: duplicate `POST /v1/assessments` (same → original ID, different → 409); an invalid edge is rejected while the assessment commits; a write after `close` → `409 investigation_cancelled`.
- Chaos (S3): cancel during an agent commit → either committed before the fence or rejected, never committed after; wipe the agent volume mid-task → `task_lost` → re-dispatch; let an agent overrun → self-cancel with no commit.
