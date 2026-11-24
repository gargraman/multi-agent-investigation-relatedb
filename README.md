# Nexus

Nexus is a design-stage MVP for multi-agent intelligence investigations: a deterministic
orchestrator delegates tasks to specialist agent nodes (SIGINT, GEOINT, Analysis), which write
provenance-tracked findings into a shared embedded graph behind a Context Service. The full
design lives in
[`specs/00-product/Nexus_MVP_Architecture_and_Design_Specification.md`](specs/00-product/Nexus_MVP_Architecture_and_Design_Specification.md).
This repository is currently a **workspace skeleton** (slice S0): structure, tooling and the
contract/schema pipeline only — no application behaviour yet.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages Python 3.12+ and all dependencies)
- GNU make

## Quick start

```bash
make sync && make test
```

Other targets: `make lint`, `make fmt`, `make schemas`, `make schemas-check`, `make help`.
Targets for later slices (`up`, `demo`, `e2e`, …) exist but exit non-zero with
"not implemented yet" until their slice lands.

## Workspace layout

| Path | Package (import) | Role |
|---|---|---|
| `contracts/` | `nexus-contracts` (`nexus_contracts`) | Pydantic models = source of truth; JSON Schemas in `contracts/schemas/` |
| `shared/` | `nexus-shared` (`nexus_shared`) | Cross-cutting libs: config, logging, telemetry, security, llm |
| `apps/orchestrator/` | `nexus-orchestrator` (`nexus_orchestrator`) | Frontend API + deterministic workflow |
| `nodes/sigint-node/` | `nexus-sigint` (`nexus_sigint`) | SIGINT specialist agent |
| `nodes/geoint-node/` | `nexus-geoint` (`nexus_geoint`) | GEOINT specialist agent |
| `nodes/analysis-node/` | `nexus-analysis` (`nexus_analysis`) | Analysis agent |
| `context-service/` | `nexus-context-service` (`nexus_context_service`) | Sole gateway to the shared graph |
| `scripts/` | — | `gen_schemas.py` (schema export + drift check) |
| `spikes/` | — | Throwaway spikes; **excluded** from the workspace |

## Dependency rule

Each service depends **only on itself + `nexus-contracts` + `nexus-shared`**. A node must never
import another node's package (or the orchestrator's, or the Context Service's); services talk
over HTTP using the contracts. `contracts` depends on nothing in the workspace; `shared` may
depend on `contracts` only.

## Contracts and schema drift

JSON Schemas are generated from the models registered in `nexus_contracts.registry` and
committed. `make test` includes a drift check; if it fails, run `make schemas` and commit.
