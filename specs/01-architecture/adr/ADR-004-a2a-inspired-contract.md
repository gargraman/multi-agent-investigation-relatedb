# ADR-004: Agent contract — A2A-inspired HTTP contract behind an adapter

- **Status:** Proposed
- **Date:** 2026-11-24
- **Spec refs:** §12.2

## Context
v1.0 claimed A2A, but its example payloads were a custom JSON shape. The A2A protocol has its own envelope, methods and Agent Card format. Claiming compliance without implementing it violates the spec's own rules.

## Decision
- Use a small versioned REST contract (`/v1/tasks`, `/v1/tasks/{id}`, `/v1/tasks/{id}/cancel`, `/.well-known/agent.json`), defined as Pydantic models in `contracts/a2a/`.
- Each element maps 1:1 to an A2A concept (Agent Card, task create/get/cancel, task states).
- The transport is isolated in `apps/orchestrator/delegation/` behind a `TaskTransport` interface.
- Documentation says "A2A-inspired" and never "A2A-compliant".

## Alternatives considered
| Option | Pros | Cons |
|---|---|---|
| **A2A-inspired REST (chosen)** | Simple; fully under our control; easy to test | Not interoperable with third-party A2A agents |
| Official A2A SDK from day one | Real interoperability; standard Agent Cards | SDK maturity and API churn risk; more concepts than P0 needs |

## Consequences
- (+) Fast to build and test; honest claims.
- (−) Swapping to the official SDK later (P1) is an adapter rewrite plus contract-mapping tests.
