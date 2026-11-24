# ADR-008: P0 workflow engine — deterministic state machine, no LangGraph

- **Status:** Proposed
- **Date:** 2026-11-24
- **Spec refs:** §4, §6.2, §8.2, §8.6, §8.7, AC13

## Context
v1.1 used LangGraph with its SQLite checkpointer to run the investigation workflow in the orchestrator. But in P0 that workflow:
- is fixed by the scenario template (no LLM planning; §4.2, §6.2);
- contains no LLM calls at all; the LLM is used only inside the agents;
- is driven by a deterministic transition table (§8.2) and wall-clock deadlines;
- spends most of its time waiting on 2 s polls.

The plain version: LangGraph is a good tool when an LLM decides what happens next. Here, a lookup table decides. Using a framework built for agent loops to run a fixed checklist adds a dependency, a checkpoint format to reason about, and a checkpoint write on every poll iteration, and buys no capability. Resuming after a restart (AC13) is simpler to prove when the state is just rows you can query.

## Decision
- The orchestrator workflow is a **plain state machine**:
  - SQLite tables `investigations`, `tasks` and `timeline_events` (spec §8.7);
  - a transition table in code, unit-tested for every allowed and rejected transition;
  - each state change is one SQLite transaction (status update guarded by `version`, plus a timeline row);
  - one asyncio poller per active investigation;
  - a **resume sweep** at startup that reloads `RUNNING`/`ANALYZING` investigations and restarts their pollers.
- The engine sits behind a `WorkflowEngine` interface (`start`, `on_task_update`, `cancel`, `resume_all`), so a different engine can replace it.
- LangGraph is reconsidered **in P1**, when LLM-assisted task planning (§3.2) makes the workflow dynamic.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Plain state machine + SQLite (chosen)** | Minimal dependencies; state is directly queryable; resume is a SQL query; trivially testable | We write ~200–300 lines of orchestration code ourselves; no built-in graph visualisation |
| LangGraph + SQLite checkpointer (v1.1) | Ready-made checkpointing; natural path to LLM planning; LangChain/LangGraph tracing integrations | Framework weight for a fixed flow; checkpoint per poll; checkpoint blobs are harder to inspect and migrate; one more API to pin |
| Temporal / durable-execution engine | Strong durability semantics | Extra server container; far beyond MVP needs |

## Consequences
- (+) Fewer dependencies; AC13 resume is easy to reason about and test with chaos tests.
- (+) The workflow state *is* the operational data; the UI and API read the same tables.
- (−) If P1 adds LLM planning, LangGraph (or similar) is introduced behind `WorkflowEngine`. Keep transitions in one module so that move stays mechanical.
- **Observability: no impact.** Langfuse (P1) traces LLM calls, which happen only in the agents, through the OpenAI SDK or OTel GenAI spans. The workflow shows up in Tempo as OTel spans. What we lose is LangGraph's automatic node-level view in Langfuse, which matters little for a fixed workflow.
