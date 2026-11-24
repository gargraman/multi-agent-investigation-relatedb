# Nexus — MVP Architecture & Design Specification

**Version:** 1.0  
**Date:** 24 November 2026  
**Purpose:** Implementation baseline for spec-driven development with an AI coding assistant  
**Deployment:** Local Docker Compose demo  
**LLM:** OpenAI API  
**Initial domain:** Fictional defense-intelligence collaboration using synthetic data only

---

## 1. Executive summary

Nexus (Distributed Agentic Intelligence Network) is a prototype for collaboration between independently running specialist agents. Each agent performs a focused analysis, contributes attributable findings to a governed shared context, and collaborates with other agents to produce an assessment.

The MVP demonstrates this workflow:

1. An operator creates a fictional investigation in the web UI.
2. A coordinator creates tasks and delegates them to independent SIGINT and GEOINT agent services.
3. Each agent analyzes its own synthetic evidence fixtures.
4. Each agent submits structured findings through the Context Service.
5. The Context Service validates provenance and policy, then persists the shared knowledge in Relate.
6. An Analysis Agent retrieves authorized findings, correlates them, preserves contradictions and creates a structured assessment.
7. The UI shows the investigation timeline, agents, findings, evidence references and assessment.
8. Grafana shows operational health and distributed traces; Langfuse adds detailed AI traces in P1.

The product must demonstrate **independent analysis, governed sharing, provenance, uncertainty and observable collaboration**. It must not be a generic chatbot with multiple agent names.

This is a local demonstration using synthetic data. It is not a certified air-gapped, classified or operational intelligence system. The application runs locally, but calls to the OpenAI API require internet access and an API credential. Never send real classified or sensitive intelligence to the external model API.

## 2. Product context and problem

In distributed or disconnected environments, useful information may be held by separate teams or systems. Each may possess only part of the evidence. Copying all raw information into one central system can create ownership, connectivity, governance and security concerns.

Nexus explores a model in which:
- specialist agents run as separate services/nodes;
- each agent has a narrow role and its own workflow;
- agents collaborate through explicit contracts;
- shared knowledge is accessed through a governed Context Service;
- every contribution preserves source, provenance, confidence and classification;
- contradictory findings remain visible;
- a coordinator assembles results into an assessment.

### 2.1 Fictional MVP scenario

Use synthetic scenario data about an unknown entity and a possible activity pattern.

| Node / agent | Responsibility | Example contribution |
|---|---|---|
| SIGINT | Analyze synthetic communication metadata/patterns | A recurring communication pattern and confidence |
| GEOINT | Analyze synthetic geospatial observations | A location/time pattern and confidence |
| Analysis | Correlate findings | An assessment, gaps and contradictions |
| Coordinator | Own investigation lifecycle | Delegates tasks and tracks completion |

Do not implement real signal interception, surveillance, targeting, weapons, operational planning or real-world identification. All names, entities, locations and evidence are fictional.

### 2.2 Product principles

1. Collaboration, not chatbot behavior.
2. Agents never access Relate files directly.
3. An observation is not automatically a verified fact.
4. Provenance is first-class.
5. Contradictions are retained, not overwritten.
6. LLMs do not enforce authorization, classification or workflow state.
7. Observability is designed in from the start.
8. Local-first application deployment with Docker Compose.
9. OpenAI credentials are supplied through environment configuration and never committed.
10. Development proceeds through small, testable specifications.

## 3. Scope and priorities

### 3.1 P0 — MVP must-have

- React + TypeScript operator UI.
- Python + FastAPI API layer.
- Coordinator and LangGraph investigation workflow.
- Three independent agent services: SIGINT, GEOINT and Analysis.
- OpenAI model integration through a small provider abstraction.
- A2A-style task communication using typed contracts and an adapter.
- Context Service as the only owner of the shared Relate store.
- MCP tool surface for Context Service if it adds value; otherwise typed internal client first.
- Synthetic evidence fixtures and repeatable demo scenario.
- Findings, evidence references, relationships, assessments and provenance.
- Investigation timeline and agent task status in the UI.
- OpenTelemetry + Grafana LGTM monitoring.
- Health checks, timeouts, bounded retries and idempotency.
- Docker Compose local deployment.
- Unit, contract, integration and end-to-end tests.

### 3.2 P1 — after core workflow works

- Langfuse self-hosted AI tracing and evaluation.
- NATS JetStream for asynchronous notifications, buffering and replay.
- Per-zone collectors and controlled telemetry forwarding.
- Advanced agent discovery and autonomous delegation.
- Evaluation datasets, quality scoring and historical comparisons.
- More complete identity, policy and audit infrastructure.
- Backup/restore automation and hardened deployment profiles.

### 3.3 Explicitly out of scope

- Certified air-gap or classified deployment.
- Real cross-domain transfer or data-diode implementation.
- Real intelligence feeds or collection.
- Real-time signal, satellite or image processing.
- Production HA or multi-region deployment.
- Commercial multi-tenant SaaS.
- Autonomous real-world actions.
- LLM-controlled access decisions.
- Foundation-model hosting or fine-tuning.

## 4. Architecture decisions

| Concern | MVP decision | Rationale / caveat |
|---|---|---|
| Deployment | Docker Compose | Reproducible local demo |
| Frontend | React + TypeScript | Interactive investigation UI |
| API | Python + FastAPI + Pydantic | Fits AI/agent ecosystem |
| Workflow | LangGraph | Explicit stateful orchestration |
| LLM | OpenAI API | Reasoning and structured output |
| Shared graph | Relate | POC graph store; validate operational limits |
| Shared data access | Context Service | Governance and validation boundary |
| Agent communication | A2A adapter/contract | Delegation and task results |
| Tool access | MCP or typed internal client | Standardized service access |
| Event bus | No requirement in first P0 path; NATS P1 | Avoid premature infrastructure |
| Observability | OpenTelemetry + Grafana LGTM | Metrics, logs and traces |
| AI observability | Langfuse P1 | LLM traces and evaluations |
| Data | Synthetic JSON fixtures | Safe and repeatable demo |
| Identity | Local demo service credentials | Not production IAM |

### 4.1 Technology responsibilities

- **A2A:** agent-to-agent task collaboration, delegation, status and results.
- **MCP:** agent-to-tool/service access.
- **Context Service:** governed shared knowledge API and tool surface.
- **Relate:** persistent shared graph.
- **LangGraph:** workflow execution and local working state.
- **OpenTelemetry:** telemetry instrumentation and correlation.
- **NATS (P1):** event notification and replay, not the source of truth.

A2A and MCP are complementary. A2A does not provide shared persistent memory and neither protocol bypasses security boundaries.

### 4.2 Collaboration topology

Use a **hybrid coordinator-led topology** for the MVP:
- coordinator owns investigation lifecycle and dispatches known tasks;
- specialist agents return results and finding references;
- agents do not require unrestricted mesh discovery in P0;
- all shared findings go through Context Service;
- A2A carries task requests, status and result references, not a copy of the full graph.

This is easier to reason about and test than a fully autonomous mesh. More autonomous discovery can be considered later.

### 4.3 Shared graph ownership

Use one Relate instance owned by a dedicated Context Service process/container.

Rules:
- only Context Service opens and writes the Relate database;
- agents never mount Relate files;
- agents use Context Service REST APIs or its MCP tools;
- Context Service validates identity, schema, provenance, classification and allowed operations;
- do not expose unrestricted Cypher or arbitrary graph writes;
- validate Relate's current persistence, concurrency, backup, memory, licensing and single-writer constraints before treating it as a production choice.

## 5. Logical architecture

```text
                  +--------------------------+
                  | React operator console   |
                  +-------------+------------+
                                |
                                v
                  +--------------------------+
                  | FastAPI API Gateway      |
                  +-------------+------------+
                                |
                                v
                  +--------------------------+
                  | Coordinator / LangGraph  |
                  +----------+---------------+
                             |
                    A2A task dispatch
                      /            \
                     v              v
             +-------------+  +-------------+
             | SIGINT node |  | GEOINT node |
             | agent       |  | agent       |
             +------+------+  +------+------+
                    \                /
                     \ findings     /
                      v            v
                  +--------------------------+
                  | Context Service          |
                  | REST / MCP tools         |
                  | policy, validation,      |
                  | provenance, audit        |
                  +-------------+------------+
                                |
                                v
                  +--------------------------+
                  | Relate shared graph      |
                  | Context Service owns it  |
                  +--------------------------+

          Analysis Agent independently retrieves context,
          correlates findings and persists an assessment.

          Observability plane:
          OTel SDKs -> Collector -> Mimir / Loki / Tempo
                                      |
                                    Grafana
          Langfuse for detailed AI traces in P1.
```

### 5.1 Runtime boundaries

Each node runs in its own container with its own process, identity, configuration, prompts, tools and local workflow state. One node must not import another node's implementation package. Cross-node communication uses documented contracts and approved transport paths.

## 6. Component responsibilities

### 6.1 Frontend

Pages:
1. **Overview:** node health, active investigations, recent activity.
2. **New Investigation:** objective, scenario template and selected agents.
3. **Investigation Detail:** timeline, task graph, findings and assessment.
4. **Knowledge Explorer:** entities, observations, evidence and relationships.
5. **System Health:** node and platform health.
6. **Demo Data / About:** synthetic-data and prototype limitations.

The UI should emphasize investigation state and collaboration. Chat can be a secondary interaction, not the main product metaphor.

### 6.2 API Gateway

- frontend-facing REST API;
- schema validation;
- investigation creation/retrieval;
- workflow start and status;
- findings and assessment views;
- local demo authentication;
- no direct browser-to-Relate access.

### 6.3 Coordinator

- creates investigation state;
- selects specialist agents;
- creates and dispatches A2A tasks;
- tracks status, deadlines and retries;
- waits for required results;
- starts analysis when policy conditions are met;
- handles partial completion and failure;
- persists workflow state/checkpoints;
- emits progress events to the UI.

Use deterministic code for state transitions. The LLM may help interpret an objective, but cannot invent task completion or bypass required steps.

### 6.4 Specialist agents

Each agent has a narrow role, typed input/output contracts, limited tools, a domain prompt, output validation, bounded retries, timeout handling and provenance metadata. SIGINT and GEOINT use synthetic fixture data only.

### 6.5 Analysis Agent

- retrieves authorized findings and entity context;
- identifies corroboration and contradiction;
- separates observation from inference;
- creates a structured assessment;
- states confidence and rationale;
- lists information gaps and follow-up questions;
- references exact finding IDs.

It must not invent evidence. It must distinguish supported conclusions from hypotheses.

### 6.6 Context Service

- owns Relate connection and lifecycle;
- exposes typed REST APIs and optionally MCP tools;
- validates entity, evidence, finding and relationship schemas;
- enforces identity and classification rules;
- records provenance and versions;
- preserves contradictory findings;
- provides context retrieval/search;
- emits audit events and telemetry;
- exposes health/readiness endpoints.

It is a knowledge-governance layer, not a thin database proxy.

## 7. Repository structure

```text
nexus/
├── apps/
│   ├── frontend/
│   └── api-gateway/
├── coordinator/
│   ├── workflows/
│   ├── state/
│   ├── delegation/
│   └── tests/
├── nodes/
│   ├── sigint-node/
│   ├── geoint-node/
│   └── analysis-node/
├── context-service/
│   ├── api/
│   ├── domain/
│   ├── repositories/
│   ├── policies/
│   ├── mcp/
│   └── audit/
├── contracts/
│   ├── a2a/
│   ├── api/
│   ├── events/
│   └── schemas/
├── shared/
│   ├── config/
│   ├── logging/
│   ├── telemetry/
│   ├── security/
│   └── testing/
├── observability/
│   ├── otel-collector/
│   ├── grafana/dashboards/
│   ├── grafana/provisioning/
│   └── alerting/
├── data/
│   ├── scenarios/
│   └── fixtures/
├── infrastructure/docker/
│   ├── compose.yaml
│   ├── compose.observability.yaml
│   ├── compose.langfuse.yaml
│   └── .env.example
├── specs/
│   ├── 00-product/
│   ├── 01-architecture/
│   ├── 02-api-contracts/
│   ├── 03-data-model/
│   ├── 04-workflows/
│   ├── 05-ui/
│   ├── 06-security/
│   ├── 07-observability/
│   └── 08-test-plans/
├── scripts/
├── docs/
├── Makefile
└── README.md
```

One node must not import another node's implementation. Shared code should be limited to contracts, schemas and genuinely common infrastructure helpers.

## 8. Main investigation workflow

1. User creates an investigation.
2. API validates and creates investigation metadata.
3. Coordinator initializes a LangGraph workflow.
4. Coordinator creates SIGINT and GEOINT tasks.
5. Tasks are dispatched to independent nodes using A2A.
6. Each agent reads its synthetic evidence fixtures.
7. Agent uses OpenAI for bounded extraction/reasoning where appropriate.
8. Output is validated against Pydantic schema.
9. Agent submits finding through Context Service.
10. Context Service validates policy/provenance and persists graph records.
11. Agent returns task result with finding IDs and summary.
12. Coordinator checks completion conditions.
13. Coordinator dispatches Analysis Agent.
14. Analysis Agent retrieves authorized context.
15. Analysis Agent correlates findings and drafts assessment.
16. Context Service persists assessment and links to supporting findings.
17. Coordinator marks investigation completed, partial or failed.
18. UI displays timeline, findings, relationships and assessment.

### 8.1 Workflow states

```text
CREATED -> VALIDATING -> DISPATCHING -> COLLECTING_FINDINGS
        -> ANALYZING -> COMPLETED

Other states:
WAITING_FOR_AGENT
PARTIAL
FAILED
CANCELLED
```

A timeout for one agent is not automatically proof that the entire investigation failed. Apply a defined retry policy, then mark the task failed and allow partial completion only if the workflow policy permits it.

### 8.2 LangGraph state

Conceptual state:

```python
class InvestigationState(TypedDict):
    investigation_id: str
    objective: str
    scenario_id: str
    status: str
    requested_agents: list[str]
    tasks: dict[str, dict]
    finding_ids: list[str]
    assessment_id: str | None
    errors: list[dict]
    trace_id: str | None
```

Keep large evidence payloads out of workflow state. Store references and retrieve content through Context Service. Select a durable checkpointer if restart recovery is required; do not claim recovery across restarts until tested.

## 9. Data model and graph

Do not collapse all information into a generic `Fact`.

| Object | Meaning |
|---|---|
| Entity | Fictional subject, location, organization or asset |
| Observation | What an agent observed in its input |
| Evidence | Reference to synthetic source material |
| Finding | Normalized, attributable intelligence contribution |
| Relationship | Typed connection between entities/findings |
| Investigation | Objective, tasks, findings and assessment container |
| Assessment | Analysis Agent's reasoned synthesis |
| Decision | Operator's recorded disposition/follow-up |

### 9.1 Provenance fields

As appropriate, every observation, finding and assessment includes:

```text
id
investigation_id
source_agent_id
source_node_id
source_evidence_ids[]
confidence
classification
created_at
valid_from
valid_until
status
version
supersedes_id
reasoning_summary
```

Use UTC timestamps. Do not store hidden chain-of-thought. Store concise rationale, evidence references, assumptions and limitations.

### 9.2 Conceptual relationships

```text
(Investigation)-[:CONTAINS]->(Finding)
(Finding)-[:SUPPORTED_BY]->(Evidence)
(Finding)-[:ABOUT]->(Entity)
(Finding)-[:CORROBORATES]->(Finding)
(Finding)-[:CONTRADICTS]->(Finding)
(Assessment)-[:USES_FINDING]->(Finding)
(Assessment)-[:BELONGS_TO]->(Investigation)
(Decision)-[:DISPOSES]->(Assessment)
(Entity)-[:RELATED_TO]->(Entity)
```

Confirm exact labels, query syntax and persistence model against the selected Relate release.

### 9.3 Contradiction handling

- Never overwrite a finding just because another agent disagrees.
- Preserve both findings.
- Use statuses such as `ACTIVE`, `DISPUTED`, `SUPERSEDED`.
- Link contradictory findings explicitly.
- Analysis describes the conflict and its effect on confidence.
- An operator can record a decision/disposition.

## 10. API and agent contracts

These are logical contracts. Create versioned OpenAPI and JSON Schema files during specification work.

### 10.1 Frontend API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/v1/investigations` | Create/start investigation |
| GET | `/api/v1/investigations` | List investigations |
| GET | `/api/v1/investigations/{id}` | Investigation details |
| GET | `/api/v1/investigations/{id}/timeline` | Workflow timeline |
| GET | `/api/v1/investigations/{id}/findings` | Findings |
| GET | `/api/v1/investigations/{id}/assessment` | Assessment |
| POST | `/api/v1/investigations/{id}/cancel` | Cancel |
| GET | `/api/v1/system/health` | Aggregated health |

Use Server-Sent Events (SSE) for live progress in P0. WebSockets are unnecessary without a bidirectional requirement.

### 10.2 Context Service API

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/findings` | Submit validated finding |
| POST | `/entities` | Create/update entity under policy |
| GET | `/entities/{id}/context` | Retrieve entity context |
| POST | `/relationships` | Add typed relationship |
| POST | `/search` | Search authorized context |
| POST | `/investigations` | Create investigation metadata |
| GET | `/investigations/{id}` | Retrieve investigation context |
| POST | `/assessments` | Persist assessment |
| GET | `/audit/{id}` | Retrieve permitted audit history |

Prefer domain operations such as `submit_finding` and `create_assessment` over arbitrary graph-write endpoints.

### 10.3 Example A2A task request

```json
{
  "task_id": "task-sigint-001",
  "investigation_id": "inv-demo-001",
  "task_type": "analyze_synthetic_communications",
  "objective": "Identify recurring patterns in the supplied synthetic communication metadata.",
  "input_refs": ["evidence-001", "evidence-002"],
  "classification": "DEMO",
  "trace_context": {"traceparent": "00-..."}
}
```

### 10.4 Example agent result

```json
{
  "task_id": "task-sigint-001",
  "agent_id": "sigint-agent",
  "status": "COMPLETED",
  "summary": "A recurring synthetic communication pattern was observed.",
  "finding_ids": ["finding-001"],
  "confidence": 0.72,
  "warnings": [],
  "completed_at": "2026-11-24T09:59:20Z"
}
```

### 10.5 Example assessment

```json
{
  "assessment_id": "assessment-001",
  "investigation_id": "inv-demo-001",
  "summary": "The synthetic observations suggest a possible association.",
  "conclusions": [{
    "statement": "The observations may describe related activity.",
    "confidence": 0.68,
    "supporting_finding_ids": ["finding-001", "finding-002"],
    "limitations": ["The observations do not independently establish identity."]
  }],
  "contradictions": [],
  "information_gaps": ["Additional independent evidence is required."],
  "status": "DRAFT"
}
```

Agent-estimated confidence is not statistically calibrated unless a calibration method has been implemented and evaluated.

## 11. OpenAI model integration

### 11.1 Appropriate model use

Use OpenAI for bounded tasks:
- interpret an investigation objective into a structured task plan if required;
- extract structured observations from synthetic fixtures;
- summarize evidence;
- identify candidate relationships;
- correlate findings and draft assessments.

Use deterministic code for identity, authorization, validation, workflow transitions, retries, persistence, provenance, classification and audit.

### 11.2 Provider abstraction

```python
class LLMProvider(Protocol):
    async def generate_structured(
        self,
        *,
        task: str,
        input_data: dict,
        output_schema: type[BaseModel],
    ) -> BaseModel:
        ...
```

Implement OpenAI first. Avoid building a complex multi-provider framework in P0.

### 11.3 Configuration

`.env.example`:

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL=<configured-model-name>
OPENAI_TIMEOUT_SECONDS=45
OPENAI_MAX_RETRIES=2

NEXUS_ENV=local
NEXUS_LOG_LEVEL=INFO
NEXUS_DEMO_MODE=true

CONTEXT_SERVICE_URL=http://context-service:8000
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

Choose the exact model at implementation time based on current account access, structured-output/tool support, latency and cost. Never hard-code credentials or commit `.env`.

### 11.4 Model output rules

- Use structured outputs/JSON Schema where supported by the selected model.
- Validate every result with Pydantic.
- Reject invalid or incomplete results.
- Use bounded retries with concise validation feedback.
- Treat fixture content and retrieved findings as untrusted data, not instructions.
- Delimit evidence from system/developer instructions.
- Set input/output token budgets and timeouts.
- Version prompts in source control.
- Do not ask for or store hidden chain-of-thought; request concise rationale and supporting evidence IDs.

### 11.5 Cost and latency

- Use a suitable smaller model for simple extraction.
- Avoid model calls for deterministic transformations.
- Cache immutable fixture parsing where appropriate.
- Set per-task token and time budgets.
- Record token counts and latency without recording sensitive content.
- Retry transient errors only; do not create unbounded semantic repair loops.
- Record model and prompt versions in traces.

**Local deployment clarification:** Docker Compose hosts the application and data locally. OpenAI API inference requires network connectivity and an API key. This is not fully offline inference. The MVP must use synthetic data only.

## 12. Observability architecture

### 12.1 Selected option A

**P0:** OpenTelemetry + Grafana LGTM:
- OpenTelemetry SDKs: instrumentation.
- OTel Collector: receive, process, redact, batch and export.
- Mimir: metrics.
- Loki: logs.
- Tempo: traces.
- Grafana: dashboards and alerting.

**P1:** Langfuse for detailed LLM/agent traces, prompt/version analysis and evaluations.

Grafana's `docker-otel-lgtm` image is a convenient POC distribution containing Grafana, Mimir, Loki, Tempo and an OTel Collector. Pin the image and verify its exact configuration. Avoid deploying duplicate collectors with ambiguous routing.

### 12.2 Instrumentation coverage

| Service | Required telemetry |
|---|---|
| API Gateway | Request count, latency, status, exceptions |
| Coordinator | Investigation lifecycle, workflow node duration, retries |
| Each agent | Task count, task duration, status, model calls |
| A2A adapter | Requests, latency, timeouts, failures |
| MCP/tool client | Tool name, duration, status, error category |
| Context Service | API latency, validation failures, graph operation latency |
| Relate boundary | Query/write duration and status |
| Collector | Received/exported telemetry, dropped data, exporter errors |
| Docker host | CPU, memory, disk and container restarts |

### 12.3 Correlation

Use W3C Trace Context (`traceparent`, `tracestate`) and consistent attributes:

```text
service.name
service.version
deployment.environment
nexus.node.id
nexus.agent.id
nexus.agent.role
nexus.investigation.id
nexus.task.id
nexus.classification
```

Do not use investigation IDs, entity names, user IDs, evidence IDs or arbitrary error messages as metric labels. They create high cardinality. Use trace attributes or structured log fields where permitted.

### 12.4 Expected trace

```text
Investigation
  ├── Coordinator: create workflow
  ├── A2A: delegate SIGINT
  │    ├── SIGINT agent execution
  │    ├── OpenAI model call
  │    ├── MCP: submit finding
  │    └── Context Service: persist finding
  ├── A2A: delegate GEOINT
  │    ├── GEOINT agent execution
  │    ├── OpenAI model call
  │    └── Context Service: persist finding
  └── Analysis agent
       ├── Retrieve shared context
       ├── OpenAI model call
       └── Persist assessment
```

Explicitly test trace propagation across A2A requests/responses. Do not assume protocol integration automatically propagates OTel context.

### 12.5 Dashboards

| Dashboard | Panels | Priority |
|---|---|---|
| Nexus Overview | Agent health, active investigations, success/failure, recent errors | P0 |
| Agent Execution | Tasks by status, p50/p95 duration, retries, failures | P0 |
| Federation / A2A | Request rate, error rate, latency, timeouts | P0 |
| Context Service | Read/write latency, failures, graph operation rate | P0 |
| Infrastructure | CPU, memory, disk, restarts, collector health | P0 |
| AI / LLM | Model calls, latency, tokens, failures, evaluations | P1; basic metrics P0 |

### 12.6 Initial alert rules

Thresholds are demo starting points and must be tuned after measuring a baseline.

| Alert | Initial condition | Severity |
|---|---|---|
| Agent unavailable | No heartbeat for 60 seconds | Critical |
| Context Service unavailable | Health check fails for 1 minute | Critical |
| A2A failure rate | >5% over 5 minutes with sufficient traffic | Warning |
| A2A latency | p95 >3 seconds for 5 minutes | Warning |
| Relate write failures | >2% over 5 minutes | Critical |
| Relate query latency | p95 >1 second for 5 minutes | Warning |
| Container memory | >85% of limit for 5 minutes | Warning |
| Container restarts | >3 in 10 minutes | Warning |
| Disk usage | >80% | Warning |
| Collector export failures | Persistent for 5 minutes | Warning |

P0 alerts: agent unavailable, Context Service unavailable, A2A failure rate, high container memory and Collector export failure.

### 12.7 Telemetry security

- Do not send raw intelligence, full prompts/responses, coordinates or sensitive source content to general telemetry.
- Redact at source and/or Collector before export.
- Use IDs and approved metadata.
- Restrict Langfuse access because it may capture prompt/response content.
- Keep security audit records in a separate protected append-only store.
- Grafana/Loki/Tempo are not authoritative security audit stores.
- A real multi-enclave deployment needs per-zone collectors and an approved telemetry export path. The central monitoring plane must not become an uncontrolled cross-zone channel.

## 13. Docker Compose architecture

### 13.1 Services

| Service | Purpose | Priority |
|---|---|---|
| frontend | React UI | P0 |
| api-gateway | FastAPI UI API | P0 |
| coordinator | LangGraph workflow | P0 |
| sigint-node | Specialist agent | P0 |
| geoint-node | Specialist agent | P0 |
| analysis-node | Assessment agent | P0 |
| context-service | Governed shared context API | P0 |
| Relate | Context Service-owned graph storage/process | P0 |
| grafana-lgtm | Metrics, logs, traces and dashboards | P0 |
| standalone otel-collector | Optional separate telemetry gateway | P0 if needed |
| langfuse + dependencies | AI tracing/evaluation | P1 |
| nats | Notifications and replay | P1 |

Langfuse's backing services vary by release. Use the official Compose configuration for the selected pinned version rather than assuming a fixed dependency list.

### 13.2 Networks

```text
nexus-application
nexus-sigint-zone
nexus-geoint-zone
nexus-analysis-zone
nexus-context
nexus-federation
nexus-observability
```

Rules:
- connect each container only to networks it needs;
- agent-to-agent communication uses the A2A adapter;
- agents access shared knowledge through Context Service;
- only Context Service owns the Relate data directory;
- observability uses an explicit management-plane path;
- monitoring network is not a general inter-agent network.

Docker Compose networks simulate logical segmentation on one host. They do not prove physical air-gap security or certified enclave isolation.

### 13.3 Compose overlays

```text
infrastructure/docker/
  compose.yaml
  compose.observability.yaml
  compose.langfuse.yaml
  .env.example
```

Commands:

```bash
# Core application
docker compose -f infrastructure/docker/compose.yaml up -d

# Add Grafana LGTM
docker compose \
  -f infrastructure/docker/compose.yaml \
  -f infrastructure/docker/compose.observability.yaml up -d

# Add Langfuse in P1
docker compose \
  -f infrastructure/docker/compose.yaml \
  -f infrastructure/docker/compose.observability.yaml \
  -f infrastructure/docker/compose.langfuse.yaml up -d
```

### 13.4 Compose requirements

- Pin image versions; no floating `latest`.
- Use health checks and readiness conditions.
- Persist Relate and observability data.
- Keep secrets out of source and Git.
- Bind admin UIs to localhost for local demo.
- Set resource limits where supported.
- Add graceful shutdown and readiness checks.
- Document reset, backup and restore.
- Validate Relate's actual supported process/serve mode and persistence before finalizing entrypoint.
- Use official pinned Langfuse Compose configuration for P1.
- Do not make telemetry export a synchronous business-operation dependency.

### 13.5 Resource planning

Planning estimates excluding model inference and model weights:

| Component group | CPU | RAM |
|---|---:|---:|
| Application, coordinator and agents | 4–6 vCPU | 6–10 GB |
| Grafana LGTM | 2–4 vCPU | 4–6 GB |
| Separate Collector, if used | 0.5–1 vCPU | 0.5–1 GB |
| Langfuse stack, P1 | 4–7 vCPU | 6–10 GB |
| OS/Docker/headroom | — | 3–5 GB |

Recommended host for application + LGTM + Langfuse: approximately **16 vCPU, 32 GB RAM and 200 GB SSD** as a starting point. A lighter development profile may run on 8 vCPU, 16 GB RAM and 100 GB SSD with fewer services or shorter retention. Measure actual consumption.

Starting retention:
- metrics: 7–15 days;
- logs: 7 days;
- traces: 3–7 days;
- Langfuse traces: 7 days;
- security audit: separate policy.

## 14. Security and trust boundaries

### 14.1 Identity and authorization

- Each agent has a stable ID and service-specific credential.
- Do not share one unrestricted credential among all agents.
- Context Service checks caller identity and permitted operation.
- Apply role/attribute and classification checks in code.
- Local demo credentials are not production IAM.
- Do not claim mTLS/PKI security unless implemented and tested.
- LLM output cannot grant itself permissions.

### 14.2 Prompt and tool safety

- Treat fixtures, findings and agent messages as untrusted input.
- Delimit evidence from instructions.
- Validate all model output.
- Allowlist tools and validate arguments.
- Do not let agents invent endpoints or bypass Context Service.
- Store concise rationale and evidence references, not hidden chain-of-thought.

### 14.3 Audit

Record caller/agent identity, operation, target object ID, timestamp, authorization outcome, result status and correlation ID. A structured append-only log may suffice for the demo if protected from normal application mutation. Production requires a separately designed tamper-evident audit solution.

## 15. Failure handling

| Failure | Expected behavior |
|---|---|
| Specialist unavailable | Timeout, bounded retry, then failed/waiting task |
| OpenAI timeout | Bounded retry with backoff; preserve task state |
| Invalid model output | Schema validation failure; bounded repair/retry; do not persist invalid output |
| Context Service unavailable | Do not claim write succeeded; keep retryable task state |
| Relate write failure | Explicit error; no success before commit |
| A2A timeout | Coordinator records timeout and applies recovery policy |
| One specialist fails | Partial investigation only if policy permits |
| Grafana/Collector unavailable | Business flow continues; telemetry failure is visible when monitoring recovers |
| Duplicate result | Idempotency key prevents duplicate finding |
| Contradictory findings | Preserve both and create contradiction link |

## 16. Testing and acceptance criteria

### 16.1 Test layers

- **Unit:** schemas, state transitions, provenance, policy, retries, contradiction handling.
- **Contract:** A2A task/result, Context Service API, frontend API, MCP schema, trace context.
- **Integration:** independent agent startup, Context Service persistence, A2A dispatch, analysis retrieval, idempotency.
- **E2E:** full investigation, UI timeline, failure/recovery, telemetry trace.

### 16.2 P0 acceptance criteria

1. `docker compose up` starts core services.
2. UI creates an investigation from a synthetic scenario.
3. Coordinator delegates to at least two independent specialist agents.
4. Both agents return typed results.
5. Findings persist through Context Service.
6. Analysis Agent retrieves those findings and creates a structured assessment.
7. Assessment references exact supporting finding IDs.
8. UI shows timeline and task states.
9. Seeded contradictions remain visible and are not overwritten.
10. Grafana shows service health, agent execution, A2A and Context Service telemetry.
11. A distributed trace spans coordinator, agents and Context Service.
12. Stopping one agent creates a visible timeout/failure state.
13. Restarting it permits recovery or a clearly reported partial result.
14. Raw evidence/full prompts are not emitted to general telemetry by default.
15. Demo content is clearly identified as synthetic.

Initial local-demo quality targets:
- typical synthetic investigation completes within a few minutes depending on model latency;
- every task has timeout and bounded retry count;
- invalid model output is never persisted as a valid finding;
- task success is not reported before required persistence succeeds;
- UI distinguishes running, completed, partial, failed and waiting;
- logs/traces include correlation identifiers;
- app remains usable if Grafana is temporarily unavailable.

## 17. P0 implementation plan

Build vertical slices; do not deploy every optional component before the first investigation works.

### Phase 0 — Specifications and repository
- create repository structure and ADRs;
- define P0/P1 scope;
- define schemas for investigation, task, finding and assessment;
- define secrets/configuration;
- create synthetic scenario fixtures.
**Exit:** specifications reviewed and schemas agreed.

### Phase 1 — Application skeleton
- React frontend, FastAPI gateway, coordinator and Context Service;
- Compose networks and health checks;
- local demo identity.
**Exit:** UI creates and retrieves a placeholder investigation.

### Phase 2 — Shared context
- integrate Relate behind Context Service;
- persist findings, entities and relationships;
- implement retrieval, provenance and idempotency;
- test contradiction handling.
**Exit:** finding can be submitted and retrieved only through Context Service.

### Phase 3 — First independent agent
- implement SIGINT container;
- define task/result contract;
- integrate OpenAI provider;
- validate structured output;
- submit finding through Context Service.
**Exit:** coordinator dispatches and receives a persisted finding.

### Phase 4 — Second agent and A2A
- implement GEOINT independently;
- add A2A adapter/contract;
- dispatch both specialist tasks;
- propagate trace context;
- handle timeout and duplicate results.
**Exit:** two nodes contribute to one investigation.

### Phase 5 — Analysis
- implement Analysis Agent;
- retrieve shared context;
- correlate findings;
- preserve contradictions and gaps;
- persist assessment with finding references.
**Exit:** end-to-end investigation works.

### Phase 6 — UI
- timeline, task status, findings, provenance, assessment and health views.
**Exit:** user understands workflow without reading logs.

### Phase 7 — Observability
- deploy Grafana LGTM;
- instrument gateway, coordinator, agents and Context Service;
- instrument A2A and tool calls;
- propagate W3C trace context;
- redact sensitive data;
- build P0 dashboards and alerts;
- test failure/recovery.
**Exit:** investigation traces and operational dashboards work.

### Phase 8 — Demo hardening
- E2E tests and deterministic fixtures;
- startup/reset scripts;
- OpenAI setup instructions;
- secret scanning/check;
- demo walkthrough.
**Exit:** a new developer can run the demo by following README.

## 18. Spec-driven development instructions for the coding LLM

Treat this document as the architecture baseline, not as permission to generate the whole application in one pass.

### 18.1 Working rules

1. Read this document fully before proposing implementation.
2. Inspect the repository before changing files.
3. Do not assume code, infrastructure or dependencies exist.
4. Identify ambiguity and contradictions before implementation.
5. Preserve P0/P1 boundaries.
6. Do not silently change architecture decisions.
7. For decisions affecting interfaces, persistence, security or deployment, present options/trade-offs and request approval.
8. Prefer small vertical slices.
9. Define acceptance criteria before coding.
10. Version contracts and schemas.
11. Add tests with every slice.
12. Update specs and ADRs after approved design changes.
13. Never create mock success paths that pretend a model call or database write occurred.
14. Use synthetic data only.
15. Do not log secrets, raw evidence or full prompts/responses.
16. Do not claim air-gap, production security or HA properties that have not been implemented and tested.

### 18.2 Required feature workflow

For every feature:

1. Understand: summarize the requirement and context.
2. Scope: list included and excluded behavior.
3. Questions: ask only blocking questions; otherwise state assumptions.
4. Specify: write functional and non-functional requirements, contracts and acceptance criteria.
5. Design: show components, sequence and failure paths.
6. Review: identify risks, trade-offs and security implications.
7. Plan: split work into small dependent tasks.
8. Implement: change only approved scope.
9. Test: run relevant unit, contract, integration and E2E tests.
10. Verify: report actual command results; never claim unrun tests passed.
11. Document: update specs, README and ADRs.

### 18.3 Feature specification template

```markdown
# Feature: <name>

## Context
Why this feature exists in Nexus.

## Goal
What outcome it provides.

## Scope
### In scope
- ...

### Out of scope
- ...

## Actors and dependencies
- ...

## Functional requirements
- FR-001: ...

## Non-functional requirements
- NFR-001: ...

## Contracts
- API request/response
- Task/event schema
- Data model changes

## Workflow
1. ...

## Failure handling
- ...

## Security and privacy
- ...

## Observability
- Required metrics, logs and spans.

## Acceptance criteria
- Given / When / Then ...

## Test plan
- Unit
- Contract
- Integration
- End-to-end

## Implementation tasks
- [ ] ...
```

### 18.4 Definition of done

A feature is done only when:
- specification is approved;
- interfaces are documented;
- code is within approved scope;
- success and important failure paths are tested;
- authorization and validation exist where required;
- telemetry is present where required;
- documentation is updated;
- feature can be demonstrated locally.

### 18.5 LLM communication style

- Explain in plain language first, then technical detail.
- Prefer concise tables and diagrams.
- State assumptions explicitly.
- Be critical about overengineering, missing requirements and unsafe shortcuts.
- Do not invent library capabilities or claim protocol compliance without evidence.
- Verify version-specific details against official documentation.
- Isolate uncertain libraries behind adapters.
- Ask one focused question at a time for blocking decisions.
- Do not ask again for decisions already settled here.

## 19. Open decisions before implementation

| Decision | Recommended MVP default | Why verify |
|---|---|---|
| Exact OpenAI model | Current model with structured-output/tool support | Account access, cost, latency |
| A2A implementation | Maintained compatible SDK if suitable; otherwise internal adapter | Verify maturity and API |
| MCP scope | Context Service tools only | Keep P0 small |
| Relate deployment | Context Service-owned process/storage | Verify release-specific serving/persistence |
| Workflow checkpoint store | Start simple; add durable store if restart recovery required | Checkpoint backend choice |
| UI component library | Select one | Avoid design-system scope creep |
| Progress transport | SSE | Confirm implementation complexity |
| Demo identity | Local service tokens | Replace before production |
| NATS | P1 | Not needed for synchronous first demo |
| Langfuse | P1 | Add after OTel baseline |
| Per-zone collectors | P1 / production direction | Compose is not real enclave isolation |

## 20. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Too much infrastructure before workflow | MVP delay | Vertical slices; NATS/Langfuse P1 |
| Relate limitations | Reliability/scale risk | Validate early; repository interface |
| Inconsistent LLM output | Invalid findings | Structured output, Pydantic, bounded retries |
| Prompt injection in fixture data | Unsafe agent behavior | Treat evidence as untrusted; tool allowlist |
| Sensitive telemetry | Data leakage | Redaction, classification, access controls |
| Broken A2A trace continuity | Poor troubleshooting | Explicit propagation tests |
| Context Service failure | Shared context unavailable | Clear failure state and recovery |
| OpenAI unavailable | Tasks fail | Bounded retry; never fake success |
| Compose mistaken for air-gap | Security overclaim | Document limitation clearly |
| Agent output treated as fact | Misleading assessment | Provenance, confidence, uncertainty |

## 21. Demo script

A 5–8 minute walkthrough:

1. Introduce Nexus and the distributed-agent problem.
2. Show SIGINT, GEOINT and Analysis as separate services.
3. Create an investigation from a synthetic scenario.
4. Show coordinator delegating separate tasks.
5. Show each agent submitting findings with source and confidence.
6. Show shared context through Context Service/knowledge view.
7. Show Analysis Agent correlating findings and highlighting uncertainty.
8. Show assessment with supporting finding references.
9. Open Grafana trace and follow the workflow across services.
10. Stop one agent, show timeout/alert, restart it and demonstrate recovery or partial completion.

Core narrative: independent analysis, governed sharing, provenance, uncertainty and observable collaboration.

## 22. Documentation to verify during implementation

Use current official documentation for version-specific details; this design is not a replacement for vendor docs.

- OpenAI API: https://platform.openai.com/docs/
- LangGraph: https://langchain-ai.github.io/langgraph/
- MCP: https://modelcontextprotocol.io/
- A2A: https://a2a-protocol.org/
- OpenTelemetry: https://opentelemetry.io/docs/
- Grafana: https://grafana.com/docs/
- Grafana LGTM Docker distribution: https://github.com/grafana/docker-otel-lgtm
- Langfuse: https://langfuse.com/docs
- FastAPI: https://fastapi.tiangolo.com/
- Docker Compose: https://docs.docker.com/compose/

Verify Relate's current official documentation and selected release's operational model before implementing exact query, persistence and serving details.

## 23. Final implementation guidance

Build one complete path first:

**Create investigation → delegate to two specialist agents → persist findings through Context Service → correlate findings → generate assessment → inspect provenance and traces.**

Keep the first release narrow: synthetic evidence, coordinator-led hybrid topology, one shared Context Service, OpenAI API, Docker Compose and Grafana LGTM. Add NATS and Langfuse only after the core workflow and trace propagation are stable.

Keep the LLM provider, A2A transport, MCP tools and graph repository behind interfaces so technologies can be replaced without rewriting the domain model.

**The MVP succeeds when it demonstrates trustworthy, traceable collaboration between independent agents—not when it contains the largest number of frameworks.**
