# Architecture Decision Records — Nexus MVP

| ADR | Title | Status |
|---|---|---|
| [ADR-001](ADR-001-graph-store-relate-embedded.md) | Graph store: Relate embedded in the Context Service | Accepted (R1–R6, after S0 spike) |
| [ADR-002](ADR-002-operational-state-and-audit.md) | Operational state: SQLite per service; audit + idempotency in Relate | **Accepted** (A1–A5) |
| [ADR-003](ADR-003-async-task-execution.md) | Task execution: async submit/poll with agent-persisted task state | **Accepted** (B1–B7, B9) |
| [ADR-004](ADR-004-a2a-inspired-contract.md) | Agent contract: A2A-inspired HTTP contract behind an adapter | **Accepted** (C1–C3) |
| [ADR-005](ADR-005-service-topology.md) | Service topology: merged orchestrator (API + workflow) | **Accepted** (D1–D5) |
| [ADR-006](ADR-006-llm-replay-provider.md) | LLM substitute: labelled ReplayLLMProvider, replay mode only | **Accepted** (E1–E8) |
| [ADR-007](ADR-007-finding-owned-assertions.md) | Agent facts in the graph: finding-owned Assertion nodes | **Accepted** (G1–G11) |
| [ADR-008](ADR-008-p0-workflow-engine.md) | P0 workflow engine: deterministic state machine, no LangGraph | **Accepted** (F1–F9) |

Status values: Proposed → Accepted → Superseded. A Proposed ADR blocks the slice that depends on it until it is accepted.
