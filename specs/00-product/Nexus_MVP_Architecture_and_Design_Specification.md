# Nexus — MVP Architecture & Design Specification

**Version:** 1.2 (supersedes v1.1; earlier versions archived at `docs/archive/`)
**Date:** 24 November 2026
**Purpose:** Implementation baseline for spec-driven development with an AI coding assistant
**Deployment:** Local Docker Compose demo
**LLM:** OpenAI API
**Shared graph:** Relate (Neo4j's embedded graph database), embedded in the Context Service
**Initial domain:** Fictional defense-intelligence collaboration using synthetic data only

> **What changed in v1.2:** see [Appendix B — Changelog](#appendix-b--changelog-v11--v12). In short: LangGraph has been dropped from P0, the task/finding contracts have been fixed for retries and budgets, spatial and contact facts now hang off findings, and S0 is smaller. v1.1 changes are in [Appendix A](#appendix-a--changelog-v10--v11). Decisions marked **(Proposed)** are recorded as ADRs under `specs/01-architecture/adr/` and need sign-off before the affected slice starts.

---

## 1. Executive summary

Nexus is a prototype for collaboration between independently running specialist agents. Each agent performs a focused analysis, contributes attributable findings to a governed shared context, and collaborates with other agents to produce an assessment.

The MVP demonstrates this workflow:

1. An operator creates a fictional investigation in the web UI from a synthetic scenario.
2. The orchestrator creates tasks and delegates them to independent SIGINT and GEOINT agent services.
3. Each agent analyzes **its own** synthetic evidence fixtures. Patterns are detected in deterministic code, and the LLM classifies and explains them.
4. Each agent submits structured findings through the Context Service.
5. The Context Service validates identity, provenance and classification, then persists the shared knowledge in **Relate**, embedded in the Context Service process.
6. An Analysis Agent retrieves authorized findings, correlates them, preserves contradictions and creates a structured assessment.
7. The UI shows:
   - the investigation timeline, agents and findings;
   - **provenance trails, contradiction views, evidence-retraction impact and a knowledge graph view**, all answered by graph queries.
8. Grafana shows operational health and distributed traces. Langfuse adds detailed AI traces in P1.

The product must demonstrate **independent analysis, governed sharing, provenance, uncertainty and observable collaboration**. It must not be a generic chatbot with multiple agent names. It must also show **why a graph is the right shared store** (§10).

This is a local demonstration using synthetic data. It is not a certified air-gapped, classified or operational intelligence system. The application runs locally, but calls to the OpenAI API require internet access and an API credential. Never send real classified or sensitive intelligence to the external model API.

## 2. Product context and problem

In distributed or disconnected environments, useful information may be held by separate teams or systems. Each may possess only part of the evidence. Copying all raw information into one central system can create ownership, connectivity, governance and security concerns.

Nexus explores a model in which:
- specialist agents run as separate services/nodes;
- each agent has a narrow role and its own workflow;
- agents collaborate through explicit contracts;
- shared knowledge is accessed through a governed Context Service;
- **raw evidence never leaves the owning node**: only references, hashes and normalized findings are shared;
- every contribution preserves source, provenance, confidence and classification;
- contradictory findings remain visible;
- an orchestrator assembles results into an assessment.

### 2.1 Fictional MVP scenario

Use synthetic scenario data about fictional entities and a possible activity pattern in a **fictional region**.

| Node / agent | Responsibility | Example contribution |
|---|---|---|
| SIGINT | Analyze synthetic communication metadata | A recurring contact pattern between fictional handles, with confidence |
| GEOINT | Analyze synthetic geospatial observations (points in a fictional grid) | A co-location / time pattern, with confidence |
| Analysis | Correlate findings | An assessment, gaps and contradictions |
| Orchestrator | Own the investigation lifecycle | Delegates tasks and tracks completion |

Do not implement real signal interception, surveillance, targeting, weapons, operational planning or real-world identification. All names, entities, locations and evidence are fictional. Real-world datasets must not be used for content (§11.2).

### 2.2 Product principles

1. Collaboration, not chatbot behavior.
2. Agents never access Relate directly: not its files, not its Bolt/HTTP/MCP server.
3. An observation is not automatically a verified fact.
4. Provenance is first-class, including model and prompt lineage.
5. Contradictions are retained, not overwritten.
6. LLMs do not enforce authorization, classification or workflow state.
7. **Deterministic detection, LLM narration:** code finds candidate patterns, and the LLM classifies and explains them.
8. **The graph must earn its place:** at least three graph-native queries are visible live in the UI.
9. Observability is designed in from the first slice.
10. Local-first application deployment with Docker Compose.
11. OpenAI credentials are supplied through environment configuration and never committed.
12. Development proceeds through small, testable specifications.

## 3. Scope and priorities

### 3.1 P0 — MVP must-have

| Area | P0 item |
|---|---|
| UI | React + TypeScript (shadcn/ui + Tailwind, **Proposed**). Pages: New Investigation, Investigation Detail (timeline, tasks, findings, assessment, provenance drawer, retraction impact), Knowledge Explorer (minimal, read-only), System Health/About. Persistent "SYNTHETIC DATA" banner. |
| Orchestrator | **One** FastAPI service combining the frontend API and a deterministic investigation workflow: an explicit state machine with SQLite persistence and an asyncio poller (**Proposed**, ADR-005, ADR-008). |
| Agents | Three independent agent containers: SIGINT, GEOINT, Analysis. |
| Agent protocol | **A2A-inspired** async task contract over HTTP, mapped 1:1 to A2A concepts (**Proposed**, ADR-004). |
| Context Service | The only owner of the shared Relate store (embedded, in-process; ADR-001). Typed REST client for agents. |
| Operational state | SQLite per service (orchestrator, each agent). Audit and idempotency records live in Relate, written in the same transaction (**Proposed**, ADR-002). |
| LLM | OpenAI via a small provider abstraction. `ReplayLLMProvider`, clearly labelled, for tests and demo fallback (ADR-006). |
| Data | Scenario pack (6 scenarios), seeded noise generator, scenario manifest with an expected-outcome oracle. |
| Graph value | Provenance trail, evidence-retraction impact, contradiction neighbourhood, cross-agent hidden link, minimal graph view (§10). |
| Observability | OTel traces/logs/metrics from slice S1. Grafana LGTM with 2 dashboards and 2 alerts. |
| Reliability | Health/readiness, task deadlines, one retry layer per hop, idempotency, partial completion, restart recovery. |
| Deployment | Docker Compose, arm64 + amd64 images, `lite` profile without LGTM. |
| Tests | Unit, contract, integration, E2E (Playwright), chaos (stop/restart agent), canary-redaction and trace-span tests. |

### 3.2 P1 — after the core workflow works

- MCP tool surface for the Context Service (governed domain tools only, never raw Cypher).
- Langfuse self-hosted AI tracing and evaluation.
- NATS JetStream for asynchronous notifications, buffering and replay.
- Official A2A SDK / full protocol compliance, if the internal contract proves limiting.
- LLM-assisted task planning from free-text objectives. LangGraph is reconsidered here, behind the `WorkflowEngine` interface (ADR-008).
- Decision object workflow beyond retraction (operator dispositions).
- GraphRAG-style subgraph retrieval for Analysis (§10, I5), plus an evaluation against flat retrieval.
- Remaining dashboards and alerts from v1.0 §12.5/12.6.
- CI pipeline (lint, unit, contract, schema drift, secret scan).
- IEEE VAST Challenge fictional datasets as an extra scenario source, after a licence check.
- Real entity resolution beyond manifest aliases.
- Per-zone collectors and controlled telemetry forwarding.
- More complete identity, policy and separate tamper-evident audit infrastructure.
- Backup/restore automation and hardened deployment profiles.

### 3.3 Explicitly out of scope

- Certified air-gap or classified deployment.
- Real cross-domain transfer or data-diode implementation.
- Real intelligence feeds, collection or real-world datasets as content.
- Real-time signal, satellite or image processing.
- Production HA or multi-region deployment (the Context Service is single-replica by design).
- Commercial multi-tenant SaaS.
- Autonomous real-world actions.
- LLM-controlled access decisions.
- Foundation-model hosting or fine-tuning.

## 4. Architecture decisions

| Concern | MVP decision | Rationale / caveat | ADR |
|---|---|---|---|
| Deployment | Docker Compose | Reproducible local demo | — |
| Frontend | React + TypeScript + shadcn/ui | Small design-system scope | — |
| API + workflow | Single `orchestrator` service: FastAPI + deterministic state machine | One fewer hop and service. Splitting it later is mechanical. | ADR-005 |
| Workflow engine | Plain state machine + SQLite + asyncio poller; **no LangGraph in P0** | The P0 workflow is fixed and has no LLM in it, so a framework adds weight but no capability. Resume is a SQL query. | ADR-008 |
| LLM | OpenAI Responses API, strict JSON Schema output | Structured output; replay provider for tests | ADR-006 |
| Shared graph | **Relate, embedded in-process** in the Context Service | Single-process embedded DB matches the "single owner" rule | ADR-001 |
| Operational state | SQLite per service; audit + idempotency in Relate | No extra container; no dual-write for findings | ADR-002 |
| Task execution | Async tasks: submit → 202 → poll; agent persists task state | LLM latency; restart recovery without a broker | ADR-003 |
| Agent communication | A2A-inspired HTTP contract | Avoid unproven compliance claims; swappable adapter | ADR-004 |
| Tool access | Typed REST client (P0); MCP (P1) | Keep P0 small | — |
| Event bus | None in P0; NATS P1 | Polling + SSE suffice | — |
| Observability | OpenTelemetry + Grafana LGTM | Metrics, logs and traces | — |
| AI observability | Langfuse P1 | LLM traces and evaluations | — |
| Data | Synthetic scenario pack + seeded generator | Safe, deterministic, testable | — |
| Identity | Static per-service tokens + policy matrix | Not production IAM | — |

### 4.1 Technology responsibilities

- **A2A-inspired contract:** orchestrator-to-agent task delegation, status, cancellation and results.
- **Context Service:** the governed shared-knowledge API. It is the only code that runs Cypher.
- **Relate:** the persistent shared graph (findings, entities, evidence refs, relationships, assessments, audit).
- **Workflow engine (orchestrator):** a deterministic state machine. Investigation and task state live in orchestrator SQLite, which is the resume point after a restart (ADR-008).
- **OpenTelemetry:** telemetry instrumentation and correlation.
- **MCP (P1):** agent-to-tool access to Context Service domain operations.
- **NATS (P1):** event notification and replay, not the source of truth.

### 4.2 Collaboration topology

Use a **coordinator-led topology**:
- the orchestrator owns the investigation lifecycle and dispatches tasks fixed by the scenario template (no LLM planning in P0);
- specialist agents return results and finding references;
- there is no agent-to-agent mesh in P0; agents are discovered from a static registry (`config/agents.yaml`);
- all shared findings go through the Context Service;
- tasks carry references, not copies of evidence or of the graph.

### 4.3 Shared graph: Relate, embedded

**What Relate is.** Relate is Neo4j's open-source embedded graph database (Apache-2.0, written in Rust, Cypher 25). It was released on 11 Nov 2026; v1.2.x was current at the time of writing. The Python package `relate-db` runs the engine in-process (`relate.connect(path)`, `db.execute(query, params)`). Durability comes from an fsync'd write-ahead log plus snapshot checkpoints (Parquet by default). The engine supports indexes, constraints, **point indexes** and vector indexes. Its documented limits are: one graph per store, **a single user/agent per process**, and a working set that must fit in memory. An optional `relate serve` mode exposes Bolt, HTTP and MCP. Its Docker image is described as dev/test.

> These facts come from vendor documentation and the README as of Nov 2026. Slice S0 must verify them with a spike (§18) before S1 starts.

**Decision.** The Context Service imports `relate-db` and opens the store directory on a named volume. There is no separate Relate container.

Rules:
- the Context Service runs as **exactly one replica**;
- all writes are serialized through one in-process write lane (async FastAPI → dedicated worker thread). Reads may share the lane until the S0 spike confirms safe concurrent reads;
- **`relate serve` (Bolt/HTTP/MCP) is never enabled in Compose**: it exposes raw Cypher and would bypass governance;
- agents never mount the store volume;
- every write is a domain operation (e.g. `submit_finding_batch`, `create_assessment`, `retract_evidence`) executed as **one Relate transaction**, including its audit node;
- schema bootstrap (constraints, indexes) runs idempotently at Context Service startup;
- the `relate-db` version is pinned; upgrades require re-running the S0 spike suite;
- backups use `relate clone <store> <backup>` from a stopped or read-only store (`make backup`);
- portability: the same Cypher runs on a Neo4j server over Bolt. All access goes through a `GraphRepository` interface so the store can be swapped if Relate's maturity becomes a blocker.

## 5. Logical architecture

```text
              +---------------------------+
              |  React operator console   |
              +-------------+-------------+
                            | REST + SSE (same origin)
                            v
              +---------------------------+
              |  Orchestrator             |
              |  FastAPI + state machine  |
              |  [SQLite: state/timeline] |
              +------+------------+-------+
                     |  A2A-style |  tasks (submit/poll/cancel)
            +--------+            +--------+-------------+
            v                     v                      v
     +-------------+       +-------------+       +-------------+
     | SIGINT node |       | GEOINT node |       | Analysis    |
     | [own data]  |       | [own data]  |       | node        |
     | [SQLite]    |       | [SQLite]    |       | [SQLite]    |
     +------+------+       +------+------+       +------+------+
            |  findings           |  findings           | context / assessment
            +----------+----------+----------+----------+
                       v                     v
              +---------------------------+
              |  Context Service          |
              |  policy · provenance ·    |
              |  audit · graph queries    |
              |  +---------------------+  |
              |  | Relate (embedded)   |  |
              |  +---------------------+  |
              +---------------------------+

  Observability plane:  OTel SDKs -> Collector -> Mimir / Loki / Tempo -> Grafana
  External:             agents -> OpenAI API (egress network only)
```

Legend: `[own data]` = synthetic fixtures mounted read-only into that container only. `[SQLite]` = service-local operational state on its own volume.

### 5.1 Runtime boundaries

Each node runs in its own container with its own process, identity token, configuration, prompts, local state and fixtures. One node must not import another node's implementation package. Cross-node communication uses the versioned contracts in `contracts/` over the approved networks (§14.2).

## 6. Component responsibilities

### 6.1 Frontend

| Page | Content | Priority |
|---|---|---|
| New Investigation | Pick a scenario template, objective (prefilled), agents (from the template) | P0 |
| Investigation Detail | Live timeline (SSE), task status chips, findings with confidence/classification, assessment, **provenance drawer** (I1), **contradictions panel** (I3), **retract evidence → impact view** (I2), **hidden-link callout** (I4) | P0 |
| Knowledge Explorer | Read-only graph view of one investigation (Cytoscape.js or react-force-graph) (I6) | P0 (minimal) |
| System Health / About | Per-component health; synthetic-data and prototype limitations; replay-mode badge; "Why a graph" card (I7, P1) | P0 |
| Overview dashboard page | Active investigations, recent activity | P1 |

Rules:
- the UI emphasizes investigation state and collaboration; there is no chat in P0;
- **all LLM-generated text is rendered as plain text** (no raw HTML/markdown), to prevent XSS from model output;
- the UI must show distinct states for running, completed, partial, failed, timed out, cancelled and waiting.

### 6.2 Orchestrator (API + workflow)

Frontend API:
- schema validation;
- RFC 7807 errors;
- cursor pagination;
- SSE timeline stream;
- local demo operator authentication (single demo user, session cookie).

Workflow:
- loads the scenario manifest and creates the investigation;
- registers the investigation and its canonical entities with the Context Service;
- creates tasks from the scenario template and dispatches them;
- polls task status and enforces deadlines;
- adds up the `usage` reported by each task result and enforces the per-investigation token budget (§8.5);
- applies the partial-completion policy (§8.4);
- dispatches Analysis;
- marks the final state;
- writes timeline events;
- supports cancellation;
- resumes in-flight investigations after a restart (§8.6).

Deterministic code owns all state transitions. The LLM is not used by the orchestrator in P0.

### 6.3 Specialist agents (SIGINT, GEOINT)

Each agent:
- exposes the task API (§9.2);
- persists task state in its own SQLite (task row, attempt, status, result refs);
- loads **only its own** fixture files by evidence ID and verifies each file's hash against the manifest;
- runs **deterministic pre-analysis** in code:
  - SIGINT: recurring contact pairs, time windows, frequency anomalies;
  - GEOINT: co-location windows, dwell times, route repetition;
- calls the LLM to **classify and explain** the candidate patterns into a validated `FindingDraft`. The LLM never decides what data exists;
- maps entity mentions to canonical manifest entity IDs (§11.4);
- turns the detector output into structured **assertions** on each finding (`PLACED_AT`, `LINKED_TO`; §9.3). The detector supplies the time window and location; the LLM never invents them;
- submits **all findings of one task as one batch** (evidence references and assertions included) to the Context Service, keyed by `task_id` (§8.5);
- returns a task result with finding IDs and token `usage` only after the Context Service acknowledges the commit;
- honours cancellation and deadlines.

### 6.4 Analysis Agent

- Retrieves authorized findings and entity context for the investigation. Classification filtering is done by the Context Service, not the agent.
- Identifies corroboration and contradiction, and **proposes** `CORROBORATES` / `CONTRADICTS` edges. The Context Service validates and writes them.
- Separates observation from inference.
- Creates a structured assessment with confidence (number plus band), rationale, gaps and follow-up questions.
- References exact finding IDs. A **deterministic validator** rejects any assessment that cites a finding ID not present in the retrieval result of that run (anti-hallucination guardrail).
- Must not invent evidence. Must distinguish supported conclusions from hypotheses.

### 6.5 Context Service

- Owns the embedded Relate store and its lifecycle (open, schema bootstrap, checkpoint, close on SIGTERM).
- Exposes typed REST domain operations (§9.3). There is no arbitrary Cypher endpoint.
- Validates entity, evidence, finding, relationship and assessment schemas.
- Enforces caller identity, the operation allowlist and classification clearance (§14.1).
- Records provenance, versions and supersession.
- Preserves contradictory findings.
- Implements the **graph-value queries** I1–I4 as domain endpoints (§10).
- Writes an audit node in the same transaction as every mutating operation and every authorization denial.
- Enforces idempotency (§8.5).
- Rejects writes for cancelled investigations.
- Emits telemetry; exposes `/livez` and `/readyz`.

It is a knowledge-governance layer, not a thin database proxy.

## 7. Repository structure

```text
nexus/
├── apps/
│   ├── frontend/
│   └── orchestrator/            # FastAPI API + deterministic workflow
│       ├── api/
│       ├── workflow/            # state machine, transition table, poller, resume sweep
│       ├── state/               # SQLite schema + repositories
│       ├── delegation/          # A2A-style client, registry
│       └── tests/
├── nodes/
│   ├── sigint-node/             # detectors/, prompts/, api/, tests/
│   ├── geoint-node/
│   └── analysis-node/
├── context-service/
│   ├── api/
│   ├── domain/
│   ├── repositories/            # GraphRepository + Relate implementation
│   ├── queries/                 # versioned Cypher (I1–I4, retrieval)
│   ├── policies/                # authz matrix, classification
│   ├── migrations/              # idempotent schema bootstrap
│   └── audit/
├── contracts/                   # Pydantic models = source of truth
│   ├── a2a/  api/  events/
│   └── schemas/                 # generated JSON Schema / OpenAPI (CI drift check)
├── shared/
│   ├── config/  logging/  telemetry/  security/  llm/  testing/
├── config/
│   ├── agents.yaml              # static agent registry / minimal agent cards
│   └── policy.yaml              # authz matrix + clearances
├── observability/
│   ├── grafana/dashboards/  grafana/provisioning/  alerting/
├── data/
│   ├── scenarios/<id>/manifest.yaml
│   ├── fixtures/sigint/<id>/
│   ├── fixtures/geoint/<id>/
│   └── replay/<id>/             # recorded LLM responses (tests/demo fallback)
├── infrastructure/docker/
│   ├── compose.yaml  compose.observability.yaml  compose.dev.yaml
│   └── .env.example
├── specs/  (00-product … 08-test-plans, 01-architecture/adr/)
├── scripts/                     # gen_scenario.py, reset.sh, backup.sh, record_replay.py
├── docs/
├── pyproject.toml               # uv workspace
├── Makefile
└── README.md
```

Packaging: a **uv workspace**. `contracts`, `shared` and each service are workspace members, and each service builds its own image containing only its own member plus `contracts` and `shared`. One node must not import another node's implementation.

## 8. Investigation workflow

### 8.1 Main flow

1. The operator picks a scenario and submits the investigation.
2. The orchestrator validates the input, loads `manifest.yaml` (IDs and metadata only) and creates the investigation in SQLite (`CREATED`).
3. The orchestrator calls Context Service `POST /v1/investigations`, which creates the Investigation node and registers the canonical entities from the manifest.
4. The orchestrator creates SIGINT and GEOINT tasks from the template and submits them with `attempt = 1` and a `token_budget` (`POST /v1/tasks` → `202`). Investigation → `RUNNING`.
5. Each agent persists the task, then loads and hash-checks its fixtures.
6. The agent runs deterministic pre-analysis, then makes a bounded LLM call to classify and explain the candidates.
7. The output is validated against the Pydantic schema. If invalid: one repair attempt, then the task fails.
8. The agent calls `POST /v1/findings:batch` with every finding from the task. The Context Service validates policy and provenance, then writes the findings, their assertions and edges, and the audit node in **one transaction**.
9. The agent marks the task `COMPLETED` with finding IDs and `usage`, only after step 8 is acknowledged.
10. The orchestrator polls tasks until all are terminal or the deadline passes.
11. The orchestrator applies the partial policy (§8.4), then submits the Analysis task. Investigation → `ANALYZING`.
12. The Analysis agent retrieves authorized context (`POST /v1/search`, `GET /v1/investigations/{id}/context`).
13. The Analysis agent drafts the assessment and proposes relationship edges. The ID validator runs.
14. The Context Service persists the assessment, `USES_FINDING` edges, validated edges and contradiction statuses.
15. The orchestrator marks the investigation `COMPLETED`, `PARTIAL` or `FAILED`.
16. Every transition emits a timeline event (SQLite) and the UI receives it over SSE.

### 8.2 State machines

There are two separate state machines. Transitions not listed are rejected by code and covered by unit tests.

**Investigation**

```text
CREATED -> RUNNING -> ANALYZING -> COMPLETED
                  \            \-> PARTIAL
                   \-> PARTIAL (analysis skipped: policy not met)
RUNNING | ANALYZING -> PARTIAL (budget_exceeded)
any non-terminal -> FAILED | CANCELLED
```

**Task**

```text
PENDING -> DISPATCHED -> RUNNING -> COMPLETED
                               \-> FAILED
DISPATCHED|RUNNING -> TIMED_OUT (deadline)    any non-terminal -> CANCELLED
TIMED_OUT | FAILED(agent_restarted) -> DISPATCHED   (single orchestrator re-dispatch, attempt += 1, §8.5)
```

A task is identified by `task_id`; each dispatch of it is an **attempt** (`attempt` = 1, 2). Every task has at most two attempts.

`WAITING_FOR_AGENT` is a **UI-derived** label: shown when a task is `DISPATCHED` and the agent's `/readyz` is failing. It is not a stored state.

### 8.3 Task execution model (ADR-003)

- Submission is asynchronous: `POST /v1/tasks` returns `202 {task_id}` once the agent has persisted the task.
- The orchestrator polls `GET /v1/tasks/{id}` every 2 s, with jitter. Polling was chosen over callbacks because it survives restarts on either side without extra state.
- Task IDs are generated by the orchestrator. The agent deduplicates submissions on **`(task_id, attempt)`**:
  - same `task_id` and same `attempt` → returns the existing task (a safe network retry);
  - same `task_id` and a higher `attempt` → a re-dispatch. The agent cancels any earlier attempt that is still running (it may be a slow attempt the orchestrator already timed out), then starts the new one. Because both attempts share the batch idempotency key (§8.5), at most one of them can commit;
  - a lower `attempt` than the one stored → `409 stale_attempt`.
- On restart, an agent marks its own `RUNNING` tasks as `FAILED (agent_restarted)` unless the step is safe to resume. The orchestrator then applies the re-dispatch rule.

### 8.4 Partial-completion policy

- Analysis runs if **at least one** specialist task is `COMPLETED`.
- If any specialist task ended `FAILED`, `TIMED_OUT` or `CANCELLED`, the investigation ends `PARTIAL`. The assessment lists the missing sources under `information_gaps`.
- If no specialist completed, the investigation ends `FAILED`.

### 8.5 Retries, deadlines and idempotency

| Hop | Retry layer | Budget |
|---|---|---|
| Agent → OpenAI | OpenAI client: transient errors / 429 honouring `retry-after` | 2 retries, 45 s per call |
| Agent → schema validation | One repair call with concise validation feedback | 1 |
| Agent → Context Service | HTTP retry on connect error/5xx, using the same idempotency key | 2 |
| Orchestrator → agent | Re-dispatch a `TIMED_OUT` or `FAILED (agent_restarted)` task once, as `attempt = 2`, only if the agent is ready | 1 |
| Task deadline | Wall-clock, enforced by the orchestrator | 180 s specialist, 240 s analysis |
| Investigation deadline | Wall-clock | 10 min |

There is **one retry layer per hop**; layers are never nested.

**Idempotency: one task, one commit.**
- All findings from one task are submitted in **one batch** (`POST /v1/findings:batch`), written in **one transaction** under `idempotency_key = {task_id}`. Different attempts of the same task share the key, so at most one attempt's results are ever committed.
- The Context Service stores the key and the batch content hash on a `Submission` node, protected by a unique constraint. Each committed Finding links to it (`(Submission)-[:COMMITTED]->(Finding)`).
- Replaying the same key with the same content hash returns the original finding IDs.
- Replaying the same key with a different hash (a later attempt that produced different LLM output) returns `409 already_committed` along with the committed finding IDs. The agent reports those IDs as its result.
- Why a batch: per-finding keys (`{task_id}:{ordinal}`, v1.1) could mix findings from two attempts when the LLM output differs between them.

**Token budget:**
- Each task request carries a `token_budget`, which is what remains of `NEXUS_MAX_TOKENS_PER_INVESTIGATION` at dispatch time. The agent enforces it across its own calls; going over fails the task with `FAILED (budget_exceeded)`.
- Each task result reports `usage` (input tokens, output tokens, LLM call count). The orchestrator adds these up.
- The orchestrator checks the remaining budget **between tasks**. If nothing is left before Analysis, the investigation ends `PARTIAL (budget_exceeded)`. A model call that is already running is never cut off part-way.

### 8.6 Cancellation and restart

- `POST /api/v1/investigations/{id}/cancel`:
  - marks the investigation `CANCELLED`;
  - calls `POST /v1/tasks/{id}/cancel` on every non-terminal task;
  - tells the Context Service to reject further writes for that investigation, so late results get `409 investigation_cancelled`.
- On orchestrator startup, a **resume sweep** loads every investigation in `RUNNING` or `ANALYZING` from SQLite, together with its non-terminal tasks, and restarts their pollers. Deadlines are absolute timestamps, so a task whose deadline passed during the outage moves to `TIMED_OUT` straight away.
- Recovery across restarts is claimed only once the chaos tests pass (§17.3).

### 8.7 Workflow state (ADR-008)

The workflow is a deterministic state machine. Its whole state is a few SQLite tables, so "resume" means "query the tables and keep going". There are no checkpoints to deserialize.

```text
investigations(id PK, scenario_id, objective, status, status_reason,
               tokens_used, token_budget, deadline, assessment_id,
               trace_context_json, created_at, updated_at, version)
tasks(id PK, investigation_id FK, agent_id, task_type, status, status_reason,
      attempt, deadline, finding_ids_json, usage_json, error_category,
      error_code, created_at, updated_at, version)
timeline_events(seq INTEGER PK AUTOINCREMENT, investigation_id, type,
                payload_json, created_at)        # seq = SSE Last-Event-ID
```

- Each state change is **one SQLite transaction**: a status update, guarded by `version` (optimistic lock) and checked against the transition table, plus its `timeline_events` row.
- One asyncio poller per active investigation. The poller is stateless; it reads and writes only through the repository.
- A `WorkflowEngine` interface (`start`, `on_task_update`, `cancel`, `resume_all`) keeps the engine replaceable, e.g. with LangGraph in P1.
- Large payloads never enter workflow state. `errors` hold a category and code only, never raw content.

## 9. Data model, graph and contracts

### 9.1 Graph objects

| Label | Meaning | Notes |
|---|---|---|
| `Investigation` | Objective and scenario container | Mirrors orchestrator metadata; lifecycle status lives in the orchestrator. Only a `write_state: OPEN / CLOSED` flag is held here, to reject late writes |
| `Entity` | Fictional subject, handle, location, organization or asset | Canonical IDs come from the scenario manifest; `status: RESOLVED / UNRESOLVED`. `Location` entities (secondary label) are fictional grid cells with a cartesian `position` point |
| `Evidence` | **Reference** to synthetic source material | id, sha256, owner_node_id, classification, short summary. **Never raw content.** `status: ACTIVE / RETRACTED` |
| `Finding` | Normalized, attributable contribution | P0 folds "Observation" into Finding (`kind: OBSERVATION / INFERENCE`) |
| `Assertion` | One structured fact claimed by a finding: `PLACED_AT` (entity at a location in a time window) or `LINKED_TO` (entity ↔ entity in a window) | Always owned by exactly one Finding, so provenance and retraction reach it (ADR-007). Its validity comes from the owning finding's status |
| `Submission` | One committed batch of findings from one task | Holds `idempotency_key = task_id` and `content_hash` (§8.5) |
| `Assessment` | The Analysis Agent's synthesis | Holds conclusions, contradictions and gaps |
| `Decision` | Operator disposition | P0: created only by evidence retraction (I2). Full workflow P1 |
| `AuditEvent` | Append-only audit record | Written in the same transaction as the operation |

### 9.2 Provenance fields

Every Finding and Assessment includes:

```text
id, investigation_id, submission_id (findings) / task_id (assessments)
source_agent_id, source_node_id, source_evidence_ids[]
kind                      # OBSERVATION | INFERENCE  (findings)
confidence                # float 0..1
confidence_band           # LOW | MODERATE | HIGH   (mandatory; bands documented, uncalibrated)
classification            # DEMO-LOW | DEMO-MID | DEMO-HIGH
created_at, valid_from, valid_until   # UTC ISO-8601
status                    # ACTIVE | DISPUTED | SUPERSEDED | NEEDS_REVIEW
version, supersedes_id
reasoning_summary         # concise rationale, no chain-of-thought
model_id, prompt_version, schema_version     # LLM lineage
synthetic                 # always true in this MVP
```

### 9.3 Relationships

```text
(Investigation)-[:CONTAINS]->(Finding)
(Finding)-[:SUPPORTED_BY]->(Evidence)
(Finding)-[:ABOUT]->(Entity)
(Finding)-[:CORROBORATES]->(Finding)      # proposed by Analysis, validated by Context Service
(Finding)-[:CONTRADICTS]->(Finding)       # proposed by Analysis, validated by Context Service
(Finding)-[:DERIVED_FROM]->(Finding)      # inference built on other findings
(Assessment)-[:USES_FINDING]->(Finding)
(Assessment)-[:BELONGS_TO]->(Investigation)
(Decision)-[:DISPOSES]->(Evidence|Assessment)
(Finding)-[:ASSERTS]->(Assertion)        # ADR-007: facts hang off findings
(Assertion)-[:SUBJECT]->(Entity)
(Assertion)-[:OBJECT]->(Entity)           # LINKED_TO only: the other entity
(Assertion)-[:AT]->(Entity:Location)      # PLACED_AT only
(Submission)-[:COMMITTED]->(Finding)
(Entity)-[:RELATED_TO {type}]->(Entity)   # manifest-declared only (e.g. handle USED_BY person)
(AuditEvent)-[:CONCERNS]->(any)
```

**No fact-bearing edge exists between entities unless it comes from the manifest.** Everything an agent claims about entities (where something was, who contacted whom) is an `Assertion` owned by a Finding. A graph query only trusts an assertion while its finding is `ACTIVE` or `DISPUTED`. This is why retracting evidence (I2) also removes the facts that depend on it from I4. v1.1's `(Entity)-[:OBSERVED_AT]->(Location)` edge has been removed (ADR-007).

`Assertion` properties: `id`, `type` (`PLACED_AT | LINKED_TO`), `valid_from`, `valid_until` (UTC), `link_type` (for `LINKED_TO`, e.g. `CONTACT`). The detector supplies the location and time window, and GEOINT snaps each observation to its grid cell deterministically. The LLM may only choose among candidates that the detector produced.

Schema bootstrap creates:
- unique constraints on `id` for every label;
- a unique constraint on `Submission.idempotency_key`;
- indexes on `investigation_id` and `Assertion.type`;
- a point index on `Location.position`, using **cartesian** points (fictional grid, not WGS-84).

Exact DDL syntax is confirmed in the S0 spike.

### 9.4 Contradiction handling

- Never overwrite a finding because another agent disagrees.
- When Analysis proposes `CONTRADICTS` and the Context Service validates it (both IDs exist, same investigation, both visible to the caller), both findings move to `DISPUTED` deterministically.
- The assessment describes the conflict and its effect on confidence.
- Retracting evidence moves dependent findings and assessments to `NEEDS_REVIEW` (I2). Nothing is deleted.

### 9.5 Classification

| Level | SIGINT | GEOINT | Analysis | Operator |
|---|---|---|---|---|
| DEMO-LOW | write/read | write/read | read | read |
| DEMO-MID | write/read | write/read | read | read |
| DEMO-HIGH | write/read own | — | **denied** | read |

The `over-classified` scenario seeds a DEMO-HIGH finding. The Analysis retrieval must exclude it. These levels are fictional demo labels, not real classification markings.

**How the exclusion is audited (v1.2).** Retrieval *filters* records; nobody requests the hidden finding by ID, so there is no request to deny. To make AC18 testable:
- every read endpoint that applies a clearance filter (`/search`, `/context`, `/graph`) writes one `AuditEvent` per call, with `outcome: FILTERED` and `filtered_above_clearance: <count>` whenever the count is > 0. It records **counts only**, never the IDs of the hidden records (that would leak their existence in detail);
- a direct request by ID for an object above the caller's clearance (e.g. `GET /v1/audit/{id}` or a relationship proposal naming it) gets `404`, not `403`, so its existence isn't revealed. It writes an `AuditEvent` with `outcome: DENIED`;
- AC18 passes when the Analysis context omits the finding **and** a `FILTERED` audit event with `filtered_above_clearance ≥ 1` exists for that call.

## 10. Demonstrating the value of the graph

**Plain version:** a table answers "what do we know?". A graph answers "**how do we know it, and what breaks if one piece is wrong?**". Each item below is one short Cypher query behind a domain endpoint. In SQL, the same question needs recursive CTEs or several joins. **Litmus test:** I1–I4 must run live in the UI against the scenario pack. If they don't, the graph is decoration.

| # | Demo moment | Endpoint | Query sketch | Priority |
|---|---|---|---|---|
| I1 | **Provenance trail:** conclusion → findings → evidence → agent | `GET /v1/assessments/{id}/provenance` | `MATCH p=(a:Assessment {id:$id})-[:USES_FINDING]->(f)-[:SUPPORTED_BY]->(e) RETURN p` | P0 |
| I2 | **Evidence retraction impact:** mark evidence unreliable → every dependent finding, assertion and assessment, at any depth. Any hidden links (I4) that rested on those findings disappear | `POST /v1/evidence/{id}/retract` → returns the impact set | `MATCH (e:Evidence {id:$id})<-[:SUPPORTED_BY]-(f) OPTIONAL MATCH (f)<-[:DERIVED_FROM]-+(g) OPTIONAL MATCH (a:Assessment)-[:USES_FINDING]->(f) ...` (quantified path) | P0 |
| I3 | **Contradiction neighbourhood** for an entity | `GET /v1/entities/{id}/contradictions` | `MATCH (f1)-[:ABOUT]->(x {id:$id})<-[:ABOUT]-(f2) WHERE (f1)-[:CONTRADICTS]-(f2) RETURN f1,f2` | P0 |
| I4 | **Cross-agent hidden link:** SIGINT ties A to handle H, GEOINT places A at point P, and B was near P in the same window. Neither agent saw A↔B. | `GET /v1/investigations/{id}/links` | Two `PLACED_AT` assertions (A and B) whose locations satisfy `point.distance(...) < $r` with overlapping windows, plus a `LINKED_TO` assertion on A from a different agent; owning findings `ACTIVE`/`DISPUTED`; no existing A↔B assertion. Full sketch in ADR-007 | P0 |
| I5 | Graph-shaped retrieval for Analysis (bounded 2-hop subgraph instead of a flat list) | `GET /v1/investigations/{id}/context?shape=graph` | Capped expansion from investigation entities | P1 (stretch) |
| I6 | Knowledge Explorer view | `GET /v1/investigations/{id}/graph` | Nodes and edges for one investigation, classification-filtered | P0 (minimal) |
| I7 | "Why a graph" card: I1/I2/I4 in Cypher next to the SQL equivalent, plus Relate span latency | static + Tempo link | — | P1 |

Cypher queries are versioned in `context-service/queries/` and covered by integration tests against the seeded scenarios.

**Honest framing:** at demo scale (hundreds of nodes), a relational database would also be fast. The case for Relate is:
- **expressiveness** for provenance and dependency questions;
- **schema flexibility** while the model evolves;
- **zero-ops embedding**;
- **Cypher portability** to Neo4j if the system outgrows a single process.

No performance claims are made.

## 11. Data: scenarios, fixtures and feeding

### 11.1 Scenario pack

| Scenario | What it proves | AC |
|---|---|---|
| `baseline-corroboration` | Two agents corroborate; hidden link (I4) appears | 3–7 |
| `contradiction` | Conflicting time/location preserved and shown (I3) | 9 |
| `retraction` | Retract an evidence item → impact set (I2) | 16 |
| `prompt-injection` | Fixture contains "ignore previous instructions…"; output stays valid and unaffected | 17 |
| `over-classified` | DEMO-HIGH finding excluded from Analysis; denial audited | 18 |
| `canary` | Canary strings planted in fixtures never reach Loki/Tempo | 14 |

All scenarios share a fictional region (a bounding box on an invented grid), fictional handles and fictional organizations. Each scenario has 20–40 evidence items: hand-authored signal plus generated noise.

### 11.2 Sources

| Source | Verdict | Priority |
|---|---|---|
| Hand-authored scenario signal (planted patterns, contradiction, injection, canaries) | Required: the deterministic backbone of tests and the demo | P0 |
| `scripts/gen_scenario.py` (Python + Faker, **fixed seed**): noise around the signal | Required: makes detection non-trivial and reproducible | P0 |
| LLM-assisted authoring, offline, human-reviewed, committed | Allowed for narrative snippets; never generated at runtime | P1 |
| IEEE VAST Challenge datasets (fictional by design) | Allowed after a licence/terms check; reshaped into the manifest format | P1 |
| Real datasets (ADS-B, AIS, GDELT/ACLED, Enron email, etc.) | **Not allowed as content.** They conflict with "no real-world identification" | Out |
| Real basemaps (OSM, Natural Earth) | Not used in P0; the fictional grid avoids any real-place reading | Out (P0) |

### 11.3 Manifest and feeding pipeline

```text
data/scenarios/<id>/manifest.yaml
  scenario_id, version, synthetic: true, template (tasks per agent), objective
  entities:  [{id, type, display_name, aliases[]}]
  evidence:  [{id, owner_node, file, sha256, classification}]
  oracle:    expected finding kinds, expected corroboration/contradiction pairs,
             expected hidden links, expected denials, canary strings
data/fixtures/<node>/<id>/*.json     # mounted read-only into <node> ONLY
data/replay/<id>/*.json              # recorded LLM responses for ReplayLLMProvider
```

1. The orchestrator reads the manifest (metadata only) and registers the investigation and canonical entities with the Context Service.
2. Tasks carry `input_refs` (evidence IDs) from the template.
3. The agent loads each referenced file and verifies its `sha256`. On a mismatch, the task fails with `evidence_integrity_error`.
4. Deterministic detectors produce candidate patterns, then the LLM classifies and explains them.
5. The agent submits each finding with its evidence references (id, hash, owner, short summary).
6. E2E tests and a basic eval script compare the results against the `oracle`.

### 11.4 Entity resolution (P0)

1. Exact or alias match against manifest entities → canonical `entity_id`.
2. Otherwise, the LLM may propose one candidate from a **provided list** of manifest IDs. It is accepted only if that ID exists in the manifest.
3. Otherwise, create an `UNRESOLVED` Entity scoped to the investigation.

Real resolution is P1.

## 12. API and agent contracts

Contracts are Pydantic models in `contracts/`. JSON Schema and OpenAPI files are generated from them and committed; a drift check fails tests if they diverge. All services use RFC 7807 `application/problem+json` errors and `/v1` path versioning. List endpoints use cursor pagination.

### 12.1 Frontend API (orchestrator)

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v1/scenarios` | List scenario templates |
| POST | `/api/v1/investigations` | Create and start an investigation |
| GET | `/api/v1/investigations` | List (cursor) |
| GET | `/api/v1/investigations/{id}` | Details including task states |
| GET | `/api/v1/investigations/{id}/timeline` | Timeline events (cursor) |
| GET | `/api/v1/investigations/{id}/events` | **SSE** stream; supports `Last-Event-ID` (timeline sequence number) |
| GET | `/api/v1/investigations/{id}/findings` | Findings (proxied from the Context Service) |
| GET | `/api/v1/investigations/{id}/assessment` | Assessment |
| GET | `/api/v1/investigations/{id}/graph` | Knowledge Explorer data (proxy I6) |
| GET | `/api/v1/assessments/{id}/provenance` | Proxy I1 |
| POST | `/api/v1/evidence/{id}/retract` | Proxy I2 (operator action) |
| GET | `/api/v1/entities/{id}/contradictions` | Proxy I3 |
| GET | `/api/v1/investigations/{id}/links` | Proxy I4 |
| POST | `/api/v1/investigations/{id}/cancel` | Cancel |
| GET | `/api/v1/system/health` | Per-component status + overall `OK / DEGRADED / DOWN` |

The frontend is served from the same origin (nginx in Compose, Vite proxy in dev), so there is no CORS configuration.

### 12.2 Agent task API (A2A-inspired, ADR-004)

| Method | Endpoint | A2A concept | Purpose |
|---|---|---|---|
| GET | `/.well-known/agent.json` | Agent Card | id, role, task_types, version, clearance |
| POST | `/v1/tasks` | send message / create task | Submit a task → `202 {task_id, status}` |
| GET | `/v1/tasks/{id}` | get task | Status + result |
| POST | `/v1/tasks/{id}/cancel` | cancel task | Cancel |
| GET | `/livez`, `/readyz` | — | Health |

We do **not** claim A2A protocol compliance. The adapter in `apps/orchestrator/delegation/` isolates transport so the official A2A SDK can replace it (P1).

**Task request**

```json
{
  "task_id": "tsk_01JD3K8...",
  "investigation_id": "inv_01JD3K7...",
  "task_type": "analyze_synthetic_communications",
  "objective": "Identify recurring contact patterns in the supplied synthetic metadata.",
  "input_refs": ["ev-sig-001", "ev-sig-002"],
  "scenario_id": "baseline-corroboration",
  "classification_ceiling": "DEMO-MID",
  "attempt": 1,
  "token_budget": 20000,
  "deadline": "2026-11-24T10:03:00Z",
  "contract_version": "1.0",
  "trace_context": {"traceparent": "00-...", "tracestate": ""}
}
```

**Task result**

```json
{
  "task_id": "tsk_01JD3K8...",
  "agent_id": "sigint-agent",
  "status": "COMPLETED",
  "summary": "A recurring synthetic contact pattern was observed between two fictional handles.",
  "finding_ids": ["fnd_01JD3K9..."],
  "warnings": [],
  "attempt": 1,
  "usage": {"input_tokens": 3120, "output_tokens": 640, "llm_calls": 1},
  "model_id": "<configured-model>",
  "prompt_version": "sigint-extract@1.2.0",
  "completed_at": "2026-11-24T09:59:20Z",
  "error": null
}
```

`error` is `{category, code}` (e.g. `{"category":"llm","code":"schema_invalid"}`) and never contains raw content.

### 12.3 Context Service API

| Method | Endpoint | Caller | Purpose |
|---|---|---|---|
| POST | `/v1/investigations` | orchestrator | Create Investigation + register manifest entities |
| GET | `/v1/investigations/{id}/context` | analysis, orchestrator | Authorized findings, entities, evidence refs |
| POST | `/v1/investigations/{id}/close` | orchestrator | Mark cancelled/closed; reject further writes |
| POST | `/v1/findings:batch` | sigint, geoint | Submit **all** findings of one task (evidence refs, `ABOUT` edges, assertions) in one transaction, idempotent on `task_id` (§8.5) |
| POST | `/v1/entities` | sigint, geoint | Create an UNRESOLVED entity |
| POST | `/v1/relationships` | analysis | Propose CORROBORATES / CONTRADICTS / DERIVED_FROM (validated) |
| POST | `/v1/search` | analysis | Structured filter: investigation, entity, kind, status, classification ≤ clearance. No free-text/vector in P0 |
| POST | `/v1/assessments` | analysis | Persist assessment + USES_FINDING (ID validator re-checked server-side) |
| GET | `/v1/assessments/{id}/provenance` | orchestrator | I1 |
| POST | `/v1/evidence/{id}/retract` | orchestrator (operator) | I2: mark RETRACTED, create Decision, flag NEEDS_REVIEW, return impact set |
| GET | `/v1/entities/{id}/contradictions` | orchestrator | I3 |
| GET | `/v1/investigations/{id}/links` | orchestrator | I4 |
| GET | `/v1/investigations/{id}/graph` | orchestrator | I6 |
| GET | `/v1/audit/{object_id}` | orchestrator | Permitted audit history |
| GET | `/livez`, `/readyz` | all | Health (`readyz` = store open + schema bootstrapped) |

### 12.4 Assessment example

```json
{
  "assessment_id": "asm_01JD3KA...",
  "investigation_id": "inv_01JD3K7...",
  "summary": "The synthetic observations suggest a possible association between ENT-ALPHA and ENT-BRAVO.",
  "conclusions": [{
    "statement": "ENT-ALPHA and ENT-BRAVO may be coordinating activity near LOC-GRID-14.",
    "kind": "INFERENCE",
    "confidence": 0.62,
    "confidence_band": "MODERATE",
    "supporting_finding_ids": ["fnd_sig_001", "fnd_geo_002"],
    "limitations": ["Co-location does not independently establish coordination."]
  }],
  "contradictions": [{
    "finding_ids": ["fnd_geo_002", "fnd_geo_005"],
    "description": "Two observations place ENT-ALPHA in different grid cells in overlapping windows.",
    "effect_on_confidence": "Lowered from HIGH to MODERATE."
  }],
  "information_gaps": ["No independent corroboration of ENT-BRAVO's communications."],
  "status": "DRAFT",
  "model_id": "<configured-model>",
  "prompt_version": "analysis-assess@1.0.0"
}
```

Confidence values are model estimates and are **not statistically calibrated**.

## 13. OpenAI model integration

### 13.1 Appropriate model use

| Use | Where |
|---|---|
| Classify and explain candidate patterns from deterministic detectors | SIGINT, GEOINT |
| Propose an entity match from a provided list of manifest IDs | SIGINT, GEOINT |
| Correlate findings, propose edges, draft the assessment | Analysis |
| Interpret free-text objectives into task plans | **P1 only** |

Deterministic code handles detection, identity, authorization, validation, workflow transitions, retries, persistence, provenance, classification and audit.

### 13.2 Provider abstraction

```python
class LLMProvider(Protocol):
    async def generate_structured(
        self, *, task: str, prompt_version: str,
        input_data: dict, output_schema: type[BaseModel],
        budget: TokenBudget,
    ) -> LLMResult:  # validated model + usage (tokens, latency, model_id)
        ...
```

| Implementation | Use | Rule |
|---|---|---|
| `OpenAIProvider` | Live demo and normal runs | Responses API with strict JSON Schema output |
| `ReplayLLMProvider` | Tests, CI, demo fallback | Serves recorded responses from `data/replay/`. Responses are recorded by `scripts/record_replay.py` from real runs. **Selected only when `NEXUS_LLM_MODE=replay`.** The UI shows a "REPLAY MODE" badge, and the timeline marks every replayed call. Never silently substituted (ADR-006). Lookup key defined below. |

**Replay lookup key (v1.2).** A recording is found by `sha256(canonical_json(key_material))`, where `key_material = {scenario_id, agent_id, task_type, prompt_version, schema_version, call_ordinal, normalized_input}`:
- `normalized_input` is the exact `input_data` passed to the provider, with **volatile fields removed**: generated IDs (`inv_…`, `tsk_…`, `fnd_…`), timestamps that aren't part of the evidence, `attempt`, `trace_context` and budgets. Detectors are deterministic, so their candidate output is stable for a given scenario;
- `call_ordinal` tells apart the first call from the repair call (0 = primary, 1 = repair);
- canonical JSON means sorted keys, no whitespace, UTF-8 and fixed float formatting;
- each recording stores its key material next to the response, so a miss can be diagnosed by diffing. A miss raises `ReplayMissError` with the expected key; nothing is ever fabricated.

### 13.3 Configuration

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL_EXTRACT=<smaller-structured-output-model>
OPENAI_MODEL_ANALYSIS=<stronger-structured-output-model>
OPENAI_TIMEOUT_SECONDS=45
OPENAI_MAX_RETRIES=2
NEXUS_LLM_MODE=live            # live | replay
NEXUS_MAX_TOKENS_PER_INVESTIGATION=60000

NEXUS_ENV=local
NEXUS_LOG_LEVEL=INFO
NEXUS_MAX_CONCURRENT_INVESTIGATIONS=2

CONTEXT_SERVICE_URL=http://context-service:8000
OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4317

# per-service tokens are generated by `make secrets` into .env, never committed
```

- Every service validates its config at startup. In `live` mode, a missing API key means the service refuses to start.
- Choose exact models at slice S2 based on account access, structured-output support, latency and cost.

### 13.4 Model output rules

- Use strict JSON Schema structured outputs, and validate every result with Pydantic.
- Reject invalid or incomplete results. Allow one repair attempt with concise validation feedback, then fail the task.
- Treat fixture content and retrieved findings as **untrusted data**. Delimit them from instructions, and never execute instructions found in them (`prompt-injection` scenario).
- Enforce per-call and per-investigation token budgets as described in §8.5: the agent enforces the task's `token_budget`; the orchestrator adds up the reported `usage` and checks it between tasks. Running out ends the investigation as `PARTIAL (budget_exceeded)`.
- Prompts are versioned files in source control (`nodes/<node>/prompts/<name>@<semver>.md`).
- Never ask for or store hidden chain-of-thought. Request concise rationale and evidence IDs.

### 13.5 Cost and latency

- Use a smaller model for extraction and classification, and a stronger model for Analysis only.
- Deterministic pre-analysis shrinks LLM input to candidate patterns, not whole fixtures.
- Record token counts, latency, model ID and prompt version in spans and metrics. **Never record content.**
- A typical investigation targets under 3 minutes in live mode and under 20 s in replay mode.

**Local deployment clarification:** Docker Compose hosts the application and data locally. OpenAI inference requires network connectivity and an API key. This is not offline inference.

## 14. Security and trust boundaries

### 14.1 Identity and authorization

- Each service has a stable ID and its own bearer token. Tokens are generated by `make secrets` into the git-ignored `.env`.
- The Context Service authorizes every call against `config/policy.yaml`:

| Caller | Allowed Context Service operations | Max classification |
|---|---|---|
| orchestrator | investigations create/close/context, provenance, retract, contradictions, links, graph, audit | DEMO-HIGH (operator view) |
| sigint-agent | findings submit, entities create | DEMO-HIGH (own findings only) |
| geoint-agent | findings submit, entities create | DEMO-MID |
| analysis-agent | context, search, relationships propose, assessments create | DEMO-MID |

- The operator authenticates as a single local demo user (session cookie on the orchestrator).
- LLM output cannot grant permissions. The ID validator and the policy check run server-side.
- Do not claim mTLS/PKI security unless implemented and tested.

### 14.2 Networks (simulated segmentation)

| Network | Members | Purpose |
|---|---|---|
| `nexus-app` | frontend, orchestrator | UI ↔ API |
| `nexus-federation` | orchestrator, sigint, geoint, analysis | Task delegation only |
| `nexus-context` | context-service, orchestrator, sigint, geoint, analysis | Governed knowledge access |
| `nexus-egress` | sigint, geoint, analysis | Outbound OpenAI only (the only non-internal network) |
| `nexus-obs` | all services, lgtm | OTLP export. Services expose only `/livez`, `/readyz` and metrics there; app ports bind to other networks |

The agents share `nexus-federation` and `nexus-context`, but they expose no inbound endpoints other than the task API. Agent-to-agent calls are rejected because the task API only accepts the orchestrator's token. Docker networks simulate logical segmentation on one host. They do not prove isolation.

### 14.3 Prompt, tool and UI safety

- Treat fixtures, findings and agent messages as untrusted input.
- Delimit evidence from instructions, and validate all model output.
- Agents can only call the Context Service domain endpoints. There is no generic HTTP tool and no Cypher.
- Render LLM text as plain text in the UI.

### 14.4 Audit

- Every mutating operation and every authorization denial writes an `AuditEvent` **in the same Relate transaction** as the operation (or alone, for denials).
- An `AuditEvent` records caller ID, operation, target ID, timestamp, authorization outcome, result and trace ID.
- `AuditEvent` nodes have no update or delete endpoint. They are append-only by API construction.
- **Known deviation:** the audit lives in the same store as the data it audits. That is acceptable for a demo and documented in ADR-002. Production requires a separate tamper-evident store.

## 15. Observability architecture

### 15.1 Stack

- OpenTelemetry SDKs (auto-instrumentation for FastAPI and httpx, plus manual spans).
- `grafana/otel-lgtm`, pinned by version and digest: Collector, Mimir, Loki, Tempo, Grafana.
- `compose.observability.yaml` is an overlay. The `lite` profile runs without it, and the app works without it.
- Telemetry export is asynchronous and batched. It is never a dependency of a business operation.

### 15.2 Instrumentation (P0)

| Service | Telemetry |
|---|---|
| Orchestrator | HTTP RED; investigation lifecycle counters; task dispatch/poll spans; deadline/timeouts |
| Agents | Task count/duration/status; detector duration; LLM call spans (`gen_ai.*` semantic conventions: model, token counts, latency, **no content**) |
| Context Service | HTTP RED; validation failures; authz denials; Relate operation spans (`db.system=relate`, operation name only, never parameters) |

### 15.3 Correlation

- W3C `traceparent`/`tracestate` are propagated orchestrator → agent (in the task body **and** the HTTP header) → Context Service.
- Logs are JSON and include `trace_id` and `span_id`.
- Common attributes: `service.name`, `service.version`, `deployment.environment`, `nexus.node.id`, `nexus.agent.id`, `nexus.agent.role`, `nexus.investigation.id`, `nexus.task.id`, `nexus.classification`.
- Investigation, task, entity and evidence IDs are **never metric labels**.

### 15.4 Expected trace

```text
POST /api/v1/investigations                      (orchestrator)
  ├── workflow.create
  ├── a2a.submit sigint  ──► sigint: task.run
  │                              ├── detector.recurring_contacts
  │                              ├── gen_ai.generate (model, tokens)
  │                              └── http POST /v1/findings:batch ─► context: submit_finding_batch
  │                                                                └── relate.tx
  ├── a2a.submit geoint  ──► geoint: task.run (same shape)
  └── a2a.submit analysis ─► analysis: task.run
                                 ├── http POST /v1/search ──► context: search
                                 ├── gen_ai.generate
                                 └── http POST /v1/assessments ──► context: create_assessment
```

Polling is asynchronous, so agent spans are linked to the task-submission span through the propagated `traceparent`. A test asserts that the spans land in one trace (§17.3).

### 15.5 Dashboards and alerts

| Item | P0 | P1 |
|---|---|---|
| Dashboards | **Nexus Overview** (component health, investigations by final state, task outcomes, error rate); **Agents & Delegation** (task duration p50/p95, timeouts, LLM latency/tokens, Context Service write latency/failures) | Infrastructure, AI/LLM detail, Federation detail |
| Alerts | **Agent unavailable** (`readyz` probe fails 60 s); **Context Service unavailable** (`readyz` fails 60 s) | A2A failure rate, latency, Relate failures, memory, restarts, disk, collector export |

Health probes are implemented by the Collector's `httpcheck` receiver, which scrapes each service's `/readyz`. Container CPU/memory metrics (cAdvisor or `docker_stats`) are P1.

### 15.6 Telemetry security

- Never send raw evidence, prompts/responses, coordinates or fixture text to telemetry.
- Allowlist span attributes at the source, and add a Collector `attributes`/`redaction` processor as a second layer.
- Fixtures contain **canary strings**. An automated test asserts they never appear in Loki or Tempo (AC14).
- Grafana, Loki and Tempo are not audit stores.

## 16. Docker Compose architecture

### 16.1 Services

| Service | Purpose | Priority |
|---|---|---|
| frontend | React UI (nginx, same-origin proxy to the orchestrator) | P0 |
| orchestrator | API + deterministic workflow (SQLite volume) | P0 |
| sigint-node / geoint-node / analysis-node | Agents (SQLite volume each; fixtures mounted read-only for sigint/geoint) | P0 |
| context-service | Governed API + **embedded Relate** (store volume) | P0 |
| lgtm | `grafana/otel-lgtm`, pinned (overlay) | P0 |
| langfuse, nats | — | P1 |

That is 6 application containers plus 1 observability container. The separate Relate container and the separate API gateway from v1.0 are removed.

### 16.2 Requirements

- Pin images by version and digest; images are multi-arch (arm64 + amd64).
- Use `depends_on: condition: service_healthy` ordering: context-service, then agents, then orchestrator, then frontend.
- Admin UIs (Grafana) bind to `127.0.0.1` only.
- Set resource limits on every container.
- Graceful shutdown: on SIGTERM, the Context Service finishes the in-flight transaction, checkpoints and closes the store.
- Use named volumes for the Relate store, SQLite files and LGTM data.
- `compose.dev.yaml` adds source mounts and hot reload.

### 16.3 Developer commands

```bash
make secrets   # generate per-service tokens into .env (git-ignored)
make up        # core app (lite profile)
make up-obs    # core + Grafana LGTM
make demo      # up-obs + open UI; checks OPENAI_API_KEY or offers replay mode
make reset     # stop, wipe Relate store + SQLite volumes (confirmation prompt)
make backup    # relate clone of the store to ./backups/<timestamp>
make test      # unit + contract + integration (replay mode)
make e2e       # Playwright + chaos + telemetry tests (replay mode)
make logs      # tail all services
```

### 16.4 Resource planning

| Profile | Containers | Target host |
|---|---|---|
| `lite` (daily dev) | 6 app containers | 4 vCPU, 8 GB RAM, Apple Silicon or x86 |
| `full` (demo) | 6 app + LGTM | 8 vCPU, 16 GB RAM, 50 GB SSD |
| `full + Langfuse` (P1) | + Langfuse stack | 16 vCPU, 32 GB RAM |

These are estimates; measure actual use in slice S6. Retention: metrics 7 days, logs 3 days, traces 3 days (demo).

## 17. Failure handling and testing

### 17.1 Failure behaviour

| Failure | Expected behaviour |
|---|---|
| Specialist unavailable at dispatch | Submit retried per §8.5; task `TIMED_OUT` at deadline; UI shows "waiting for agent" while `readyz` fails |
| Agent restarts mid-task | Agent marks the task `FAILED (agent_restarted)`; orchestrator re-dispatches once; else partial policy |
| Orchestrator restarts | Resume sweep reloads `RUNNING`/`ANALYZING` investigations from SQLite and restarts pollers; tasks whose deadline passed during the outage → `TIMED_OUT` |
| Slow attempt 1 completes after re-dispatch | Batch idempotency on `task_id`: whichever attempt commits first wins; the other gets `409 already_committed` and reports the committed IDs |
| OpenAI timeout / 429 | Client retries per budget; then task `FAILED (llm_unavailable)`; never fake success |
| Invalid model output | One repair; then `FAILED (schema_invalid)`; nothing persisted |
| Cited finding ID not retrieved | Assessment rejected (`unknown_finding_reference`); one repair; then fail |
| Context Service unavailable | Agent retries with the same idempotency key; task never reports success without a commit ack |
| Relate write failure | Transaction rolled back; explicit `5xx`; audit of the failure attempted separately |
| Duplicate submission | Same key + same content → original IDs; different content → `409` |
| Late result after cancel | `409 investigation_cancelled`; nothing persisted |
| Fixture hash mismatch | `FAILED (evidence_integrity_error)` |
| Token budget exceeded | Investigation `PARTIAL (budget_exceeded)` |
| LGTM unavailable | Business flow continues; exporters drop with bounded queues |

### 17.2 Test layers

| Layer | Scope | Tooling |
|---|---|---|
| Unit | State machines, policy matrix, provenance, idempotency, detectors, ID validator, entity resolution, config validation | pytest |
| Contract | Pydantic ↔ generated schema drift; task API; Context Service API; frontend API | pytest + schemathesis |
| Integration | Context Service + real embedded Relate (temp dir): atomic writes, constraints, I1–I4 queries against seeded scenarios, authz denials + audit | pytest |
| E2E | Full investigation per scenario in replay mode; oracle comparison; UI flows | Playwright + pytest |
| Chaos | `docker stop geoint-node` mid-run → `TIMED_OUT` + `PARTIAL`; restart → re-dispatch/recovery; restart orchestrator mid-run → resume | pytest + docker SDK |
| Telemetry | Trace contains spans from ≥4 services (Tempo API); canary strings absent (Loki + Tempo APIs) | pytest |
| Live smoke | One `baseline-corroboration` run with the real OpenAI API (manual / `make smoke-live`) | pytest |

### 17.3 P0 acceptance criteria and traceability

| # | Criterion | Slice | Proof |
|---|---|---|---|
| 1 | `make up` starts core services and all `/readyz` are green | S1 | Script check |
| 2 | UI creates an investigation from a synthetic scenario | S1 | Playwright |
| 3 | Orchestrator delegates to ≥2 independent specialist agents | S3 | Integration + trace |
| 4 | Both agents return typed results | S2–S3 | Contract tests |
| 5 | Findings persist only through the Context Service | S2 | Integration; no other service mounts the store volume (compose lint) |
| 6 | Analysis retrieves findings and creates a structured assessment | S4 | E2E |
| 7 | Assessment references exact, retrieved finding IDs | S4 | ID-validator unit test + E2E |
| 8 | UI shows timeline and distinct task states | S5 | Playwright |
| 9 | Seeded contradictions preserved (both `DISPUTED` + `CONTRADICTS` edge) and visible | S4–S5 | Integration + Playwright |
| 10 | Grafana shows health, agent execution and Context Service telemetry | S6 | Provisioned dashboards; Grafana API smoke |
| 11 | One trace spans orchestrator, agents and Context Service | S1 → S6 | Tempo API test |
| 12 | Stopping an agent produces a visible timeout/failure | S3 | Chaos test |
| 13 | Restart permits recovery, or a clearly reported `PARTIAL` | S3 | Chaos test |
| 14 | Raw evidence and prompts absent from telemetry | S6 | Canary test |
| 15 | Demo content clearly identified as synthetic | S5 | Banner + `synthetic: true` everywhere |
| 16 | Retracting evidence shows the impact set and flags dependents `NEEDS_REVIEW` | S4–S5 | Integration + Playwright |
| 17 | Prompt-injection fixture does not alter agent behaviour | S2 | E2E vs oracle |
| 18 | Over-classified finding is excluded from Analysis, and a `FILTERED` audit event with `filtered_above_clearance ≥ 1` is written for that retrieval (§9.5) | S4 | Integration |
| 19 | I1–I4 run live in the UI (graph litmus test) | S5 | Playwright |

Quality targets:
- every task has a deadline and a bounded retry count;
- invalid model output is never persisted;
- success is never reported before commit;
- logs and traces carry correlation IDs;
- the app is usable while LGTM is down.

## 18. P0 implementation plan (vertical slices)

Estimates assume one experienced developer with an AI coding assistant. Re-estimate after S2.

| Slice | Content | Exit criterion | Est. |
|---|---|---|---|
| **S0 Decisions & data** | **Relate spike** (ADR-001 criteria, plus cartesian `point.distance` / point index, quantified paths, concurrent reads); ADR sign-off; Pydantic contracts (TaskRequest/Result with `attempt`/`usage`, FindingBatch with assertions, Assessment, errors) + schema generation + drift check; uv workspace skeleton; **2 scenarios only**: `baseline-corroboration` and `contradiction` (manifest, fixtures, oracle) + seeded generator | Spike report + accepted ADRs + 2 scenarios committed + `uv sync` / `make test` green | 4–5 d |
| **S1 Walking skeleton** | Orchestrator + Context Service (embedded Relate) + frontend shell; create investigation → Investigation node; SSE timeline; OTel traces across both services; Compose `lite`; `make` targets | AC1, AC2; trace across 2 services | 4–5 d |
| **S2 First agent, real LLM** | SIGINT: task API, SQLite state, fixture hash check, detectors, OpenAI provider, validation + repair, idempotent `findings:batch` with assertions; replay recording (key per §13.2); **author `prompt-injection` scenario** | AC4 (SIGINT), AC5, AC17 | 5–6 d |
| **S3 Second agent + failure paths** | GEOINT (point data, co-location detector); deadlines, `attempt`-based re-dispatch, partial policy, token budget, cancel, orchestrator resume sweep; chaos tests | AC3, AC12, AC13 | 5–6 d |
| **S4 Analysis + graph value** | Analysis agent; classification-filtered retrieval; edge proposals; ID validator; contradiction statuses; I1–I4 + I6 endpoints and integration tests; retraction; `FILTERED` audit; **author `retraction` and `over-classified` scenarios** | AC6, AC7, AC9, AC16, AC18 | 6–7 d |
| **S5 UI depth** | Investigation Detail (timeline, chips, findings, assessment, provenance drawer, contradictions, retraction impact, hidden link), Knowledge Explorer, Health/About, banners | AC8, AC15, AC19 | 5–6 d |
| **S6 Ops & hardening** | LGTM overlay, 2 dashboards, 2 alerts, redaction processor, **author `canary` scenario**, canary + trace tests, README + demo script, secret scan, resource measurement | AC10, AC11 (full), AC14; a new developer runs the demo from the README | 4–5 d |

**Total: ≈ 6–8 weeks for one developer.** S0 grew by a day and lost four scenarios; each scenario is now authored in the slice whose AC it proves.

## 19. Spec-driven development instructions for the coding LLM

(Unchanged from v1.0 §18, except as noted below.)

### 19.1 Working rules

1. Read this document fully before proposing implementation.
2. Inspect the repository before changing files.
3. Do not assume code, infrastructure or dependencies exist.
4. Identify ambiguity and contradictions before implementation.
5. Preserve P0/P1 boundaries.
6. Do not silently change architecture decisions. Changes require an ADR update.
7. For decisions affecting interfaces, persistence, security or deployment, present options and trade-offs and request approval.
8. Prefer small vertical slices.
9. Define acceptance criteria before coding.
10. Version contracts, schemas, prompts and Cypher queries.
11. Add tests with every slice.
12. Update specs and ADRs after approved design changes.
13. Never create mock success paths that pretend a model call or database write occurred. **The labelled `ReplayLLMProvider` (ADR-006) is the only permitted substitute, and only in replay mode.**
14. Use synthetic data only.
15. Do not log secrets, raw evidence or full prompts/responses.
16. Do not claim air-gap, production security, HA or A2A protocol compliance unless implemented and tested.
17. Verify Relate-specific syntax and behaviour against its current documentation and the S0 spike results. Relate is new; do not assume Neo4j server features exist in it.

### 19.2 Required feature workflow

Understand → Scope → Questions → Specify → Design → Review → Plan → Implement → Test → Verify (report actual command results) → Document.

### 19.3 Feature specification template

```markdown
# Feature: <name>
## Context
## Goal
## Scope (In / Out)
## Actors and dependencies
## Functional requirements (FR-001 …)
## Non-functional requirements (NFR-001 …)
## Contracts (API, task/event schema, graph changes, Cypher queries)
## Workflow
## Failure handling
## Security and privacy
## Observability (metrics, logs, spans)
## Acceptance criteria (Given / When / Then)
## Test plan (Unit / Contract / Integration / E2E / Chaos)
## Implementation tasks
```

### 19.4 Definition of done

A feature is done only when:
- its specification is approved;
- interfaces are documented and the schemas are regenerated;
- the code stays within the approved scope;
- the success path and the important failure paths are tested;
- authorization and validation exist where required;
- telemetry is present where required, with no content leakage;
- documentation is updated;
- the feature can be demonstrated locally.

### 19.5 LLM communication style

- Plain language first, then technical detail.
- Prefer concise tables and diagrams.
- State assumptions explicitly.
- Be critical about over-engineering.
- Do not invent library capabilities.
- Ask one focused question at a time for blocking decisions.

## 20. Decisions register

| Decision | v1.1 position | Status |
|---|---|---|
| Graph store deployment | Relate embedded in the Context Service, single replica, write lane | Proposed — ADR-001 (confirm after S0 spike) |
| Operational store | SQLite per service; audit + idempotency in Relate | Proposed — ADR-002 |
| Task execution model | Async submit/poll; agent-persisted task state | Proposed — ADR-003 |
| A2A implementation | A2A-inspired HTTP contract behind an adapter | Proposed — ADR-004 |
| Service topology | Gateway + coordinator merged into the orchestrator | Proposed — ADR-005 |
| LLM test/demo substitute | Labelled `ReplayLLMProvider`, replay mode only | Proposed — ADR-006 |
| AC13 restart recovery | Must (SQLite state tables + resume sweep) | Proposed |
| Workflow engine | Deterministic state machine, no LangGraph in P0 | Proposed — ADR-008 |
| Agent facts in the graph | Finding-owned `Assertion` nodes; no agent-written entity↔entity edges | Proposed — ADR-007 |
| Finding idempotency | One batch per task, keyed by `task_id`; `attempt` on task requests | Proposed (v1.2, §8.5) |
| Clearance-filter audit | `FILTERED` audit event with counts; 404 on direct over-clearance access | Proposed (v1.2, §9.5) |
| UI component library | shadcn/ui + Tailwind | Proposed |
| OpenAI models | Small model for extraction, stronger for analysis; chosen at S2 | Open (S2) |
| MCP | P1 | Decided |
| Progress transport | SSE with `Last-Event-ID` | Decided |
| Demo identity | Static per-service tokens + policy matrix | Decided |
| NATS / Langfuse / per-zone collectors | P1 | Decided |

## 21. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Relate maturity** (released Nov 2026; thread-safety and limits unverified; project accepts no external PRs) | Blocking bugs with no upstream fix path | S0 spike; pin version; `GraphRepository` interface; Cypher portable to a Neo4j server (fallback) |
| Relate single-process / in-memory working set | No horizontal scale | Accepted for MVP; documented; demo data is small |
| Too much infrastructure before workflow | MVP delay | Vertical slices; 6 app containers; LGTM as overlay; NATS/Langfuse/MCP P1 |
| Graph not visibly valuable | "Why not Postgres?" undermines the demo | §10 litmus test (AC19) |
| Inconsistent LLM output | Invalid findings | Detectors first; strict schemas; Pydantic; one repair |
| Hallucinated finding references | Misleading assessment | Server-side ID validator (AC7) |
| Prompt injection in fixtures | Unsafe behaviour | Untrusted-data delimiting; no generic tools; injection scenario (AC17) |
| OpenAI outage during the live demo | Demo fails | Labelled replay mode |
| Sensitive telemetry | Data leakage | Attribute allowlist + Collector processor + canary test |
| Broken trace continuity across async polling | Poor troubleshooting | Explicit propagation + Tempo test |
| Audit co-located with data | Weaker audit guarantee | Documented deviation (ADR-002); separate store P1 |
| Compose mistaken for air-gap | Security overclaim | Documented limitation |
| Agent output treated as fact | Misleading assessment | Provenance, kind (observation/inference), confidence band, contradictions |

## 22. Demo script (6–8 minutes)

1. Introduce Nexus and the distributed-agent problem. Point out the SYNTHETIC banner.
2. System Health: SIGINT, GEOINT, Analysis and the Context Service run as separate services.
3. Create an investigation from `baseline-corroboration`.
4. Watch the live timeline as the orchestrator delegates separate tasks.
5. Findings appear, each with source agent, confidence band and classification.
6. **Hidden link (I4):** the graph connects ENT-ALPHA to ENT-BRAVO via co-location, a link neither agent produced alone.
7. Assessment → open the **provenance drawer (I1)** from a conclusion down to the evidence references.
8. Run `contradiction`: both findings are kept, marked DISPUTED, and the assessment explains the confidence impact (I3).
9. **Retract an evidence item (I2):** dependent findings and the assessment flip to NEEDS_REVIEW instantly.
10. Knowledge Explorer: the whole investigation as a graph.
11. Grafana: follow one trace across the orchestrator, agents and Context Service.
12. Stop GEOINT mid-run: timeout, then alert, then PARTIAL. Restart it and re-run to show recovery.

Core narrative: independent analysis, governed sharing, provenance, uncertainty and observable collaboration, **with the graph making provenance and impact questions trivial**.

## 23. Documentation to verify during implementation

- Relate: https://github.com/relatedb/relate · https://neo4j.com/blog/news/meet-relate-by-neo4j-an-embedded-database-for-ai-agents/ · https://graphacademy.neo4j.com/blog/three-ways-to-run-relate
- OpenAI API: https://platform.openai.com/docs/
- LangGraph (P1 only, ADR-008): https://langchain-ai.github.io/langgraph/
- A2A: https://a2a-protocol.org/
- MCP (P1): https://modelcontextprotocol.io/
- OpenTelemetry (incl. GenAI semantic conventions): https://opentelemetry.io/docs/
- Grafana LGTM: https://github.com/grafana/docker-otel-lgtm
- FastAPI: https://fastapi.tiangolo.com/
- Docker Compose: https://docs.docker.com/compose/
- Langfuse (P1): https://langfuse.com/docs

## 24. Final implementation guidance

Build one complete path first:

**Create investigation → delegate to two specialist agents → persist findings through the Context Service into Relate → correlate → assessment → inspect provenance, contradictions, retraction impact and traces.**

Keep the LLM provider, task transport and graph repository behind interfaces.

**The MVP succeeds when it demonstrates trustworthy, traceable collaboration between independent agents, and shows why a graph makes that collaboration inspectable. It does not succeed by containing the largest number of frameworks.**

---

## Appendix A — Changelog v1.0 → v1.1

| Area | v1.0 | v1.1 | Review ref |
|---|---|---|---|
| Name | "Distributed Agentic Intelligence Network" (acronym mismatch) | Name only: "Nexus" | B7 |
| Relate | Unspecified serving/persistence; separate container | Embedded in the Context Service; single replica; write lane; `relate serve` forbidden; S0 spike | A1 |
| Operational state | Undefined; three sources of truth for status | SQLite per service; audit + idempotency in Relate; orchestrator owns status | A3, C17 |
| Task model | Implicit synchronous | Async submit/poll; agent-persisted task state | A2, C29 |
| A2A | Claimed, custom shape | "A2A-inspired" contract mapped to A2A concepts; compliance not claimed | A5 |
| Topology | Gateway + coordinator separate | Merged orchestrator | D |
| MCP | P0 "if it adds value" | P1 | D |
| Entity resolution | Absent | Manifest canonical IDs + aliases + constrained LLM match | A4 |
| Evidence sharing | Ambiguous | Refs + hashes only; raw stays on the owning node | B5 |
| State machines | Mixed | Separate investigation/task enums + transition tables | C19 |
| Retries | Nested, unbounded total | One layer per hop + deadlines | C2 |
| Partial policy | Undefined | ≥1 specialist completed | C3 |
| Idempotency | Undefined key | `task_id:ordinal` + content hash + 409 | C1 |
| Cancellation / restart | Undefined | Propagated cancel; write rejection; resume sweep | C9, C20 |
| Classification | Single `DEMO` level | 3 demo levels + clearances + seeded denial | B6 |
| AuthZ | Principles | Policy matrix | C7 |
| Provenance | No model lineage | `model_id`, `prompt_version`, `schema_version`, `kind`, `confidence_band` | C18, C8 |
| Observation | Separate object, no edges | Folded into Finding (`kind`) | C6 |
| Edge creation | Unspecified | Analysis proposes, Context Service validates | C4 |
| Anti-hallucination | Implied | Server-side finding-ID validator | C5 |
| Graph value | Not addressed | §10 I1–I7 + litmus AC19 | I |
| Data | "Synthetic JSON" | Scenario pack, generator, manifest + oracle, sources policy | J, C15 |
| LLM use | Extraction from fixtures | Deterministic detection + LLM narration; replay provider; token caps; Responses API | B3, C16, C26 |
| Observability | Phase 7; 6 dashboards / 10 alerts | From S1; 2 dashboards / 2 alerts P0; httpcheck probes; canary + trace tests | B1, C12, C13, C31–C33 |
| Networks | 7, egress ignored | 5 with explicit egress | B8 |
| UI | 6 pages, markdown risk | 4 pages incl. minimal graph view; plain-text LLM output | D, C27 |
| API conventions | None | RFC 7807, cursor pagination, `/v1`, `/livez` + `/readyz` | C23, C24 |
| SSE | Mentioned | `Last-Event-ID` from the timeline sequence; same origin | C25 |
| DevEx | Makefile mention | `make` targets, dev overlay, `lite` profile, multi-arch | C34, C35 |
| Plan | 9 phases, no estimates | 7 slices with exits and estimates; AC traceability | C38, H |
| Acceptance criteria | 15 | 19 (+ retraction, injection, classification denial, graph litmus) | H |

## Appendix B — Changelog v1.1 → v1.2

Source: the v1.1 spec review on 24 Nov 2026. Gap IDs (G1–G9) refer to that review.

| Area | v1.1 | v1.2 | Ref |
|---|---|---|---|
| Workflow engine | LangGraph + SQLite checkpointer | Deterministic state machine, SQLite tables, asyncio poller, resume sweep; `WorkflowEngine` interface; LangGraph reconsidered in P1 | ADR-008 |
| Task re-dispatch | Same `task_id`, and the agent dedups on `task_id` → re-dispatch was a no-op | `attempt` in TaskRequest; agent dedups on `(task_id, attempt)`; a higher attempt cancels and supersedes the earlier one | G1, §8.3 |
| Hidden-link data path | `OBSERVED_AT` / `RELATED_TO` edges with no writer | Structured `assertions[]` on findings (`PLACED_AT`, `LINKED_TO`) | G2, §6.3, §9.3 |
| Facts and provenance | Entity-level `OBSERVED_AT` edge, outside provenance | `Assertion` nodes owned by findings; no agent-written entity↔entity edges; retraction removes them from I4 | G3, ADR-007 |
| AC18 audit | "Denial audited" on a filtered read (nothing to deny) | `FILTERED` audit event with counts; 404 + `DENIED` audit on direct over-clearance access | G4, §9.5 |
| Finding idempotency | Per-finding `{task_id}:{ordinal}` (could mix attempts) | One batch per task, keyed by `task_id`, on a `Submission` node | G5, §8.5 |
| Replay lookup | Undefined | Hash of canonical key material with volatile fields stripped | G6, §13.2 |
| Token budget | Not enforceable across services | `token_budget` in TaskRequest, `usage` in TaskResult, checked between tasks | G7, §8.5 |
| Spatial model | CRS unspecified | Cartesian points on the fictional grid; spike verifies `point.distance` + point index | §9.3, §18 |
| S0 scope | 6 scenarios + spike + contracts + workspace in 3–4 d | 2 scenarios in S0; the other 4 are authored in S2/S4/S6; 4–5 d | §18 |

**Parked (Good-to-have, not in v1.2):**
- **G8:** create UNRESOLVED entities inline in the finding batch instead of a separate `POST /v1/entities` call (it removes a non-atomic step).
- **G9:** split the finding `status` into `lifecycle` + `review_flags[]`, or define a precedence between `DISPUTED` and `NEEDS_REVIEW`. Until then: `NEEDS_REVIEW` takes precedence over `DISPUTED`, and `SUPERSEDED` over both.
