# ADR-008: P0 workflow engine — deterministic state machine, no LangGraph

- **Status:** Accepted (2026-11-24, after independent review; amendments F1–F9)
- **Date:** 2026-11-24
- **Spec refs:** §4, §6.2, §8.2, §8.3, §8.5, §8.6, §8.7, §17.1, AC13

## Context
v1.1 used LangGraph with its SQLite checkpointer to run the investigation workflow in the orchestrator. But in P0 that workflow:
- is fixed by the scenario template (no LLM planning; §4.2, §6.2);
- contains no LLM calls at all; the LLM is used only inside the agents;
- is driven by a deterministic transition table (§8.2) and wall-clock deadlines;
- spends most of its time waiting on 2 s polls.

The plain version: LangGraph is a good tool when an LLM decides what happens next. Here, a lookup table decides. LangGraph handles deterministic graphs perfectly well too, so this is not a capability argument. The reasons are **dependency weight and state you can't easily inspect**: checkpoint blobs are harder to query, migrate and reason about than rows, and resuming after a restart (AC13) is simpler to prove when state is just rows.

## Decision
- The orchestrator workflow is a **plain state machine**:
  - SQLite tables `investigations`, `tasks`, `timeline_events` and `leader` (spec §8.7);
  - a transition table in code, unit-tested for every allowed and rejected transition;
  - each state change is one SQLite transaction (status update guarded by `version`, plus a timeline row);
  - one asyncio poller per active investigation;
  - a **resume sweep** at startup and a **close reconciler** (ADR-002 A2).
- The engine sits behind a `WorkflowEngine` interface: `start`, `on_task_update`, `cancel`, `resume_all`, `reconcile`.
- Behaviour already fixed elsewhere is cited, not restated: sticky terminal states and fence-first cancel (ADR-003 B1), SSE after commit (ADR-005 D3), single worker (ADR-005 D1), SQLite settings (ADR-002 A3).
- LangGraph is reconsidered **in P1**, when LLM-assisted task planning (§3.2) makes the workflow dynamic.

### Amendments from the independent review
| # | Amendment |
|---|---|
| F1 | **The resume sweep covers `CREATED`, `RUNNING` and `ANALYZING`**, matching ADR-002 A1 and ADR-003 B9. (The first draft said `RUNNING`/`ANALYZING`; fixed here and in spec §17.1.) |
| F2 | **Dispatch intent.** `PENDING` is the persisted intent; `DISPATCHED` is written only after the agent's `202` (with `acked_at`). The resume sweep re-POSTs `PENDING` tasks with the **same** `attempt`, and the agent's `(task_id, attempt)` dedup makes that safe. A `404` counts as `task_lost` only for acknowledged tasks. A clean restart never burns the single re-dispatch, and re-dispatch goes `TIMED_OUT/FAILED → PENDING (attempt 2) → DISPATCHED`. |
| F3 | **Startup order and stale lock.** Leader lock → resume sweep → reconciler → serve, all inside the FastAPI `lifespan`, so no request arrives before resumed state is loaded. The heartbeat runs every 5 s; a heartbeat older than 15 s is stale and is taken over. A new process waits (up to 30 s) instead of failing straight away, so `kill -9` plus an immediate restart recovers without a restart loop. |
| F4 | **Concurrent writers.** On a `version` conflict a writer re-reads the row and **re-validates** its transition. If it's no longer allowed (e.g. the task became `CANCELLED`), the update is dropped and a timeline note is written. Dispatching Analysis re-checks the investigation status in the same transaction that writes the Analysis task. |
| F5 | **Budget reservations.** The engine owns the token budget: per-task `token_share` from the template (default SIGINT 0.25, GEOINT 0.25, Analysis 0.5); Analysis receives everything not yet spent; attempt 2 gets its task's reservation minus attempt 1's known usage (worst-case overshoot bounded by one share, documented). Spec §8.5. |
| F6 | **Investigation deadline transition.** At 10 minutes: cancel non-terminal tasks (fence-first), then `PARTIAL (investigation_deadline)` if ≥1 specialist completed, otherwise `FAILED (investigation_deadline)`. Spec §8.2. |
| F7 | **Honest size.** Sweep, intent re-POST, reconciler, lock, deadlines, re-dispatch, budget and cancel add up to several hundred lines plus tests, not "200–300". The S3 estimate (5–6 d) holds because this replaces LangGraph integration work; re-check it at the end of S3. |
| F8 | **Fair argument.** The "checkpoint on every poll" claim is dropped (it only applies if polling is modelled as a graph loop). The real reasons are dependency weight and opaque checkpoint state. |
| F9 | **Tracing.** One span per **state transition**, not per poll; poll requests are excluded from httpx auto-instrumentation. After a resume, new spans link to the stored `trace_context_json`, so a restarted investigation stays in one trace. |

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Plain state machine + SQLite (chosen)** | Minimal dependencies; state is directly queryable; resume is a SQL query; trivially testable | We write and test the orchestration logic ourselves (F7); no built-in graph visualisation |
| LangGraph + SQLite checkpointer (v1.1) | Ready-made checkpointing; natural path to LLM planning; LangGraph tracing integrations | Framework weight for a fixed flow; checkpoint blobs are harder to inspect and migrate; one more API to pin |
| Temporal / durable-execution engine | Strong durability semantics | Extra server container; far beyond MVP needs |

## Consequences
- (+) Fewer dependencies; AC13 resume is easy to reason about and test with chaos tests.
- (+) The workflow state *is* the operational data; the UI and API read the same tables.
- (−) The engine owns more correctness logic than a framework would hide (F2–F6), so its unit and chaos tests matter.
- (−) If P1 adds LLM planning, LangGraph (or similar) is introduced behind `WorkflowEngine`. Keep transitions in one module so that move stays mechanical.
- **Observability:** Langfuse (P1) traces LLM calls, which happen only in the agents through the OpenAI SDK or OTel GenAI spans, so dropping LangGraph has no effect there. The workflow shows up in Tempo through the manual transition spans in F9. What we lose is LangGraph's automatic node-level view, which matters little for a fixed workflow.

## Tests implied
- Unit: the transition table, including the deadline transitions; version-conflict re-validation drops a disallowed update; budget reservation arithmetic, including attempt 2.
- Chaos (S3): crash between the `PENDING` commit and the `202` → resume re-POSTs and the attempt count is unchanged; `kill -9` plus immediate restart → takeover within 15 s; cancel racing a `COMPLETED` poll → stays `CANCELLED`, Analysis not dispatched; 10-minute deadline with one specialist completed → `PARTIAL (investigation_deadline)`.
- Telemetry: a restarted investigation still produces one trace; no span per poll.
