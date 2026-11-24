# ADR-005: Service topology — merged orchestrator (API + workflow)

- **Status:** Proposed
- **Date:** 2026-11-24
- **Spec refs:** §5, §6.2, §16.1

## Context
v1.0 had a separate API Gateway and Coordinator, plus a separate Relate container. For a local single-host demo, the gateway adds one hop, one container and one more trace segment without any separation benefit, because both components share the same trust level.

## Decision
- One `orchestrator` service holds the FastAPI frontend API, the SSE stream and the deterministic investigation workflow (ADR-008; LangGraph was dropped from P0 in v1.2).
- The Relate container is removed (ADR-001).
- The P0 topology is frontend, orchestrator, sigint-node, geoint-node, analysis-node and context-service, with LGTM as an overlay.

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **Merged (chosen)** | Fewer moving parts; simpler tracing | API and workflow scale together (irrelevant for the MVP) |
| Separate gateway + coordinator | Cleaner separation for future scale | Extra service and hop; more config for no MVP benefit |

## Consequences
- (+) Lower setup and debugging cost.
- (−) Splitting later requires extracting `apps/orchestrator/api/` into its own service. Keep the module boundary clean so the split stays mechanical.
