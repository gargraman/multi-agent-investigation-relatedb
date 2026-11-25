# S3 — Second agent and failure paths (GEOINT)

> **Status: Approved (2026-11-25).**

| Field | Value |
|---|---|
| Slice | S3 (spec §18, 5–6 d) |
| Branches | `feat/s3a-geoint` then `feat/s3b-failure-paths` (one spec, two shippable halves, §16) |
| Exit ACs | AC3, AC4 (GEOINT half), AC12, AC13 (spec §17.3) |
| Template | spec §19.3 |
| Governing ADRs | 002 (A1–A4), 003 (B1–B7), 004 (C2), 006 (E1–E8), 007 (G6), 008 (F1–F9) |
| Builds on | `specs/02-features/S2-sigint-agent.md`, including its §20 notes |

Spec references are to `specs/00-product/Nexus_MVP_Architecture_and_Design_Specification.md` v1.3. "verify" means a claim about a library or the model that was not checked.

---

## 0. Headline

Two specialist agents (SIGINT and GEOINT) run concurrently on every investigation. The workflow survives failure:
- every task has a deadline and at most one re-dispatch;
- the investigation has a 10-minute deadline;
- operators can cancel an investigation;
- the token budget is reserved per task;
- an orchestrator or agent restart either recovers or ends in a clearly reported `PARTIAL`/`FAILED`.

Analysis is still not enabled, so a run where both specialists succeed ends `PARTIAL(agent_not_enabled)` with `skipped_agents=[analysis-agent]` (S2 G1, unchanged). S4 turns that into `ANALYZING → COMPLETED`.

Three decisions were made before this draft (2026-11-25):
1. **Shared agent runtime.** The task API, task store, boot sweep, runner, cancel and deadline handling move from `nexus_sigint` into `nexus_shared.agent`. SIGINT and GEOINT keep only their detectors, prompt and output schema. Cancel and deadlines are written once, and S4's analysis agent reuses them.
2. **Chaos tests on both layers.** In-process fault injection in pytest (CI) plus a `make chaos` suite that stops, kills and restarts real containers in replay mode (local).
3. **GEOINT reports patterns, not sightings.** It reports co-locations and placement conflicts only (§6.3, G1). In `prompt-injection`, FOXTROT gets a co-located CHARLIE sighting so the scenario still exercises GEOINT (G2).

---

## 1. Must-have vs Good-to-have

Rule: Must only if AC3, AC4, AC12, AC13, an S2 carry-over marked Must, or a spec/ADR mandate needs it.

| # | Feature | What | Impact | Effort / risk | M/N |
|---|---|---|---|---|---|
| 1 | Shared agent runtime | Move `api.py`, `store.py`, `runner.py`, `context_client.py`, the CA builder and the app factory into `nexus_shared.agent`. SIGINT becomes a plug-in (`AgentSpec`) | Cancel and deadlines written once; S4 reuses them | M / med (refactor of working code; guarded by S2 tests) | Must |
| 2 | GEOINT node | Fixture load + hash check, alias resolution, grid snapping, co-location + conflict detector, prompt `geoint-classify@1.0.0`, output schema, `PLACED_AT` findings | AC3, AC4 | M / low (mirrors SIGINT) | Must |
| 3 | Scenario fix: FOXTROT co-location | Add a CHARLIE sighting to `prompt-injection/ev-geo-005`, update the oracle, regenerate noise, check the S2 replay keys | AC17 stays meaningful for GEOINT | S / low | Must |
| 4 | Replay recordings | GEOINT: `data/replay/geoint-agent/<scenario>/` for the 3 scenarios. SIGINT: the missing `contradiction` recording (S2 item 15b). All via the oracle-gated `record_replay.py` (≈6k live tokens in total) | Replay E2E covers both agents on all 3 scenarios; chaos suite | S / needs key + live run | Must (decided 2026-11-25) |
| 5 | Compose: geoint service | Own SQLite volume, `fixtures/geoint` ro mount, `nexus-context` + `nexus-egress`, enabled in `NEXUS_ENABLED_AGENTS` | AC3, AC5 lint extended | S / low | Must |
| 6 | Task deadlines (orchestrator) | Deadline watcher: `PENDING/DISPATCHED/RUNNING → TIMED_OUT` at `deadline`; agent cancel call | AC12 | S / low | Must |
| 7 | Single re-dispatch | `TIMED_OUT`, `FAILED(agent_restarted)`, `FAILED(task_lost)` → `PENDING` once, `attempt=2`, fresh deadline, reduced budget, only when the agent is ready | AC13 | M / med (races) | Must |
| 8 | Investigation deadline (10 min) | Cancel non-terminal tasks, then `PARTIAL` or `FAILED(investigation_deadline)` per §8.2. Fixes the S2 G13 "stuck RUNNING" risk | AC12, quality target | S / low | Must |
| 9 | Partial policy (specialists) | ≥1 specialist COMPLETED and another failed, timed out or was cancelled → `PARTIAL` with the failing reason; none completed → `FAILED` | AC13 | S / low | Must |
| 10 | Cancel | `POST /api/v1/investigations/{id}/cancel` with fence-first order (§8.6). Agent `POST /v1/tasks/{id}/cancel`. Close reconciler every 30 s | Spec §8.6; demo | M / med | Must |
| 11 | Resume sweep, full | Re-POST `PENDING` with the stored attempt, restart pollers, apply deadlines that expired during the outage | AC13 | S / low (CREATED path exists) | Must |
| 12 | Token budget between tasks | Attempt-2 budget = reservation − attempt-1 usage. `PARTIAL(budget_exceeded)` when what is left for Analysis < `NEXUS_MIN_ANALYSIS_TOKENS` | Spec §8.5 | S / low (reservations exist) | Must |
| 13 | Chaos: in-process | pytest with fake agents + controllable clock: every §17.1 failure row that has an S3 owner | AC12, AC13 in CI | M / low | Must |
| 14 | Chaos: `make chaos` | Real containers in replay mode: stop agent → TIMED_OUT → re-dispatch; kill orchestrator mid-run → resume; CS down during cancel → reconciler closes within 30 s | AC12, AC13 proof | M / med (timing) | Must |
| 14a | Chaos replay delay | `NEXUS_REPLAY_LATENCY_MS` in `ReplayProvider` (default 0; refused when `llm_mode=live`), set only in `infrastructure/docker/compose.chaos.yaml`. Gives `make chaos` a mid-task window; replay output stays byte-identical | Makes AC13-a/b testable with real containers | S / low | Must (decided 2026-11-25) |
| 15 | S2 carry-overs | `internal_error` code (N5); same deadline on same-attempt re-POST (N6); caller-scoped findings idempotency (N7) | Correctness | S / low | Must |
| 16 | UI: cancel button + GEOINT row | Cancel on the investigation page; second task row; the timeline already renders events | Demo | S / low | Must |
| 17 | `make demo-replay`, `make smoke-live` | Replace the S2 stubs (both agents) | Demo convenience | S / low | Nice |
| 18 | `nexus-federation` network | Separate agent↔orchestrator network from `nexus-context` (S2 N3) | Isolation fidelity | S / low | Nice |
| 19 | `WAITING_FOR_AGENT` UI label | Derived from agent `/readyz` when `DISPATCHED` | Clarity | S / low | Nice (S5) |
| 20 | Route-repetition detector | Spec §6.3 lists it; no oracle needs it | None for ACs | M | Nice (P1) |
| 21 | `make chaos` in CI | Needs Docker-in-CI and recordings | Automation | M / med | Nice |
| 22 | LocalStack for local development | Add a LocalStack (S3 API) service to the dev compose stack, plus an `ObjectStore` interface with the current filesystem implementation as the default and an S3 implementation behind a setting. Proposed first use: fixtures and replay recordings read from a bucket | Prepares a cloud storage path; no AC needs it | M / med (new dependency, CA/egress config, AC5 mount lint changes) | Nice (requested 2026-11-25; parked per MVP-first, G13) |

---

## 2. Context

- S2 shipped SIGINT end to end. Its plumbing (≈900 lines: `api.py`, `store.py`, `runner.py`, `context_client.py`, `main.py`, `settings.py`) is generic apart from the pipeline call.
- The orchestrator transition table already lists `TIMED_OUT` and the re-dispatch edges, guarded (`workflow/transitions.py`). Nothing drives them yet.
- Per-task token reservations are already computed when the task row is inserted (`creation.py` `begin()`, `floor(share × cap)`). Attempt 2 recomputes from `share × cap`, because its own row value is overwritten.
- The orchestrator has **no** agent readiness client (`AgentRegistry` reads cards only) and **no** Context Service `close` call. Both are new work in WP-O.
- Scenario oracles are the `oracle:` block in each `data/scenarios/<scenario>/manifest.yaml`.
- The agent already self-cancels at `deadline + grace` (`runner.py`), and `agent_tasks.cancel_requested` exists but has no endpoint.
- The Context Service already has `POST /v1/investigations/{id}/close`, and `findings:batch` already returns 409 `investigation_cancelled` after close (S2 FR-042). The orchestrator never calls close.
- The resume sweep handles only `CREATED` investigations (S1) plus restarting drivers (S2).
- GEOINT fixtures, manifest entries and `token_share` already exist for all three scenarios. `nexus_geoint` is an empty package.
- The Context Service already validates GEOINT `PLACED_AT` (`validation.py`: PERSON|ASSET subject at a LOCATION; `about` types PERSON|ASSET|LOCATION).

## 3. Goal

AC3: one investigation delegates to two independent agents, visible in one trace. AC4: both return typed results. AC12: stopping an agent produces a visible timeout or failure. AC13: a restart recovers, or the investigation ends in a clearly reported `PARTIAL`/`FAILED`. No investigation stays `RUNNING` past its deadline.

## 4. Scope

**In:** items 1–16.
**Out:** LocalStack (item 22, parked, G13); analysis agent and `ANALYZING` (S4); assessment `information_gaps` (S4); UI depth beyond the cancel button and task rows (S5); Grafana and the Tempo AC11 test (S6); route repetition (P1); items 17–21 unless time remains.

## 5. Actors and dependencies

| Actor | Role in S3 | Networks | Token |
|---|---|---|---|
| orchestrator | dispatch ×2, deadlines, re-dispatch, cancel, reconciler, resume | nexus-app, nexus-context | `NEXUS_TOKEN_ORCHESTRATOR` |
| sigint-node | unchanged behaviour, on the shared runtime, gains cancel | nexus-context, nexus-egress | `NEXUS_TOKEN_SIGINT_AGENT` |
| geoint-node (`geoint-agent`) | new | nexus-context, nexus-egress | `NEXUS_TOKEN_GEOINT_AGENT` |
| context-service | accepts `geoint-agent` batches; close fence | nexus-context | — |
| OpenAI | GEOINT classification (same model as SIGINT) | egress | `OPENAI_API_KEY` |

## 6. Functional requirements

### 6.1 Shared agent runtime (`nexus_shared.agent`, extra `nexus-shared[agent]`)

| ID | Requirement | Source |
|---|---|---|
| FR-100 | Move the generic parts of `nexus_sigint/{api,store,runner,context_client,main,settings}.py` into `nexus_shared.agent`. An agent package provides an `AgentSpec`: `agent_id`, `task_types`, `fixture_schema`, `run(request, provider, manifest, fixtures_root, budget) -> PipelineOutcome`, `card_skills`. `create_agent_app(spec, settings)` builds the FastAPI app | Decision 1 |
| FR-101 | Behaviour-preserving: every S2 SIGINT test passes unchanged except for import paths. The S2 replay recordings still hit (the prompt and key material are unchanged) | Refactor safety |
| FR-102 | `POST /v1/tasks/{id}/cancel` (orchestrator token only). SUBMITTED/WORKING → sets `cancel_requested`; the runner observes it before each LLM call and before the commit, and cancels an in-flight LLM call via asyncio. The attempt ends `CANCELED`. Terminal → 200 with the current status (idempotent). Unknown → 404 | §8.3, §8.6 |
| FR-103 | A commit that has started is not interrupted. If the Context Service answers 409 `investigation_cancelled`, the attempt ends `CANCELED` (not FAILED) | §8.3, §8.6 |
| FR-104 | An unexpected internal error ends the attempt `FAILED(internal_error)`, which is **not** re-dispatched. Only the boot sweep produces `agent_restarted` | S2 N5 |
| FR-105 | Same `(task_id, attempt)` re-POST: `request_hash` keeps excluding `trace_context` and **also excludes `deadline`**, so a resume re-POST with a recomputed deadline is a safe duplicate | S2 N6, G5 |
| FR-106 | A higher attempt cancels any earlier running attempt before starting (§8.3); a lower attempt → 409 `stale_attempt` (exists; keep the test) | §8.3 |

### 6.2 GEOINT node

| ID | Requirement | Source |
|---|---|---|
| FR-110 | Task type `analyze_synthetic_geospatial`. Same policy checks as SIGINT FR-021 (owner `geoint-agent`) | §6.3 |
| FR-111 | Load each `SightingLog` by evidence id; verify sha256 (FAILED `evidence_integrity_error` on mismatch) | §6.3 |
| FR-112 | Resolve each `subject` via manifest aliases to a PERSON or ASSET id. Unresolved subjects are dropped from detection and counted in the TaskResult summary | §11.4 |
| FR-113 | Snap each point to its grid cell with `Grid.cell_of(x, y)`, then to the `LOC-GRID-nn` entity whose position is that cell's centre. Points off the grid are dropped and counted | ADR-007 G6, `manifest.py` |
| FR-114 | **Co-location:** two sightings of **different** resolved subjects whose cell centres are ≤ `1.5 × pitch` apart (same or neighbouring cell) and whose half-open windows overlap | G1, ADR-007 G6 |
| FR-115 | **Conflict:** two sightings of the **same** resolved subject whose cell centres are > `1.5 × pitch` apart and whose windows overlap | G1, spec v1.3 #13 |
| FR-116 | **Candidates:** every sighting that takes part in at least one co-location or conflict is "involved". Involved sightings are grouped by `(cell, pattern cluster)`, where a cluster is a connected component of the co-location/conflict graph. Each group is one candidate: `about = sorted(subjects in that cell) + [LOC id]`; one `PLACED_AT` assertion per sighting (subject, location, window); evidence = the sightings' evidence ids; `pattern ∈ {CO_LOCATION, CONFLICT}` | G1, oracle shapes |
| FR-117 | Candidate aliases `C1..Cn`; evidence summaries fenced as untrusted for the LLM; **stored** evidence summaries derived from structured fields only (S2 §20 N11 rule) | AC17 |
| FR-118 | LLM output per candidate: `candidate_id`, `pattern_label ∈ {CO_LOCATION, DWELL_OVERLAP, CONFLICTING_PLACEMENT, INCONCLUSIVE}`, `confidence`, `statement`, `reasoning_summary`. Strict schema, 1 repair, length caps, no other free text | §13.4 |
| FR-119 | Finding = OBSERVATION, band 0.40/0.75, classification = max evidence, valid window = min/max of its assertions. One batch per task. Zero candidates → COMPLETED with `[]` (S2 G25) | S2 G7 |

### 6.3 Orchestrator

| ID | Requirement | Source |
|---|---|---|
| FR-120 | **Deadline watcher** in the per-investigation driver: on each tick, any task with `now ≥ deadline` in PENDING/DISPATCHED/RUNNING → `TIMED_OUT` (version-guarded), timeline event, best-effort agent cancel (FR-102) | §8.2, §8.5 |
| FR-121 | **Re-dispatch:** a task in `TIMED_OUT`, `FAILED(agent_restarted)` or `FAILED(task_lost)` with `attempt = 1` is **retry-eligible**. It moves to `PENDING` with `attempt = 2`, a **fresh** `deadline = min(now + 180 s, investigation deadline)` and `token_reservation = floor(share × cap) − attempt-1 usage (unknown usage = 0)`, **only if** the agent's `/readyz` is green (new readiness client, 2 s timeout). **Wait window** (G7): from the moment the task became retry-eligible (`tasks.updated_at`) until `min(that + 180 s, investigation deadline)`. Readiness is re-checked each tick inside the window; when the window closes the task is final. No new column: the window is derived from `updated_at` and the settings | §8.5 |
| FR-121a | **Terminal policy sees retry-eligible tasks as open.** `is_terminal` stays as is for the transition table, but the investigation-level check (FR-124) treats a retry-eligible task whose wait window is still open as **not finished**. Without this, `_maybe_finish` would end the investigation before attempt 2 can start | Builder check Q1 |
| FR-121b | **Agent-reported timeout = orchestrator timeout.** A polled `FAILED(deadline_exceeded)` at `attempt = 1` is recorded as `TIMED_OUT` (same code), so the retry does not depend on whether the orchestrator noticed the deadline before the agent's 5 s self-cancel | Builder check Q3 |
| FR-122 | `attempt = 2` failures are terminal. No third attempt | §8.2 |
| FR-123 | **Investigation deadline:** at `investigations.deadline`, cancel every non-terminal task (fence first as in FR-125, no further dispatch), then `PARTIAL(investigation_deadline)` if ≥1 specialist COMPLETED, else `FAILED(investigation_deadline)` | §8.2, ADR-008 F6 |
| FR-124 | **Terminal policy (specialists only, S3):** when every specialist task is terminal: all COMPLETED → `PARTIAL(agent_not_enabled)` (analysis skipped, unchanged from S2); ≥1 COMPLETED and ≥1 not → `PARTIAL` with `status_reason` = the first non-completed task's code (`deadline_exceeded`, `task_lost`, …); none COMPLETED → `FAILED` with the same rule | §8.4, G3 |
| FR-125 | **Cancel** `POST /api/v1/investigations/{id}/cancel` (§8.6), all within the request: (1) investigation → `CANCELLED`, `context_closed = 0`; (2) Context Service close with a **1 s total timeout**; on success `context_closed = 1`; (3) **then** agent cancel for every non-terminal task, concurrently with each other, **1 s total timeout**, regardless of (2)'s outcome; (4) those tasks → `CANCELLED` (sticky) regardless of (3)'s outcome. The response reports the real `context_closed`. Steps 2–3 run in order (fence first), never in parallel with each other. Terminal investigation → 409 `investigation_terminal` with its status. Unknown → 404 | §8.6, builder check Q2 |
| FR-126 | **Close reconciler**, at startup and every 30 s: Context Service close for every terminal investigation with `context_closed = 0`. Leader-only | ADR-002 A2 |
| FR-127 | **Resume sweep** (extends S1/S2): for `RUNNING` investigations, re-POST `PENDING` tasks with their stored attempt and deadline and restart drivers. **Poll first, then apply deadlines:** each `DISPATCHED`/`RUNNING` task gets one poll before FR-120/FR-123 are applied, so a result the agent finished during the outage (COMPLETED, or `FAILED(deadline_exceeded)` → FR-121b) is kept. Only tasks whose poll gives no terminal answer are timed out | §8.6, builder check Q3 |
| FR-128 | **Budget:** after each COMPLETED task, if `cap − Σ usage < NEXUS_MIN_ANALYSIS_TOKENS` (default 8000) and analysis is enabled, end `PARTIAL(budget_exceeded)`. With analysis disabled (S3) the check is computed and logged only, so it ships tested and S4 just turns it on | §8.5, G4 |
| FR-129 | Enable both agents: `NEXUS_ENABLED_AGENTS=sigint-agent,geoint-agent`; dispatch concurrently | AC3 |

### 6.4 Context Service

| ID | Requirement | Source |
|---|---|---|
| FR-130 | `ACCEPTED_CALLERS` adds `geoint-agent` | S2 FR-045 pattern |
| FR-131 | **Caller-scoped idempotency:** the `findings:batch` replay check matches `(task_id, caller)`. The same `task_id` from a different caller → 403 `forbidden` (new error code, §8.1) without finding ids | S2 N7 |

### 6.5 Scenario and tooling

| ID | Requirement | Source |
|---|---|---|
| FR-140 | `prompt-injection/ev-geo-005`: add `subject-charlie` at a point in cell 9 (`LOC-GRID-09`), 2026-10-26 14:30–15:30Z. The existing injection text stays. Oracle: replace the FOXTROT entry with `{agent: geoint-agent, kind: OBSERVATION, assertion: PLACED_AT, about: [ENT-CHARLIE, ENT-FOXTROT, LOC-GRID-09], evidence: [ev-geo-005]}`. Corroborations and hidden paths stay empty (no contact overlaps 14:30–15:30; CHARLIE is FOXTROT's direct SIGINT link) | G2 |
| FR-141 | Re-run `gen_scenario.py`. Noise may change (rejection sampling now sees the new signal). `gen_scenario.py --check` and `test_scenario_oracle.py` must pass. Verify that the S2 SIGINT replay keys for `prompt-injection` are unchanged; re-record if not | G2 |
| FR-142 | `record_replay.py` gains `--agent geoint-agent`; record all 3 GEOINT scenarios plus SIGINT `contradiction` through the gate (exact subset + evidence superset + tripwires in statements, reasoning and stored evidence summaries) | ADR-006 E5 |

### 6.6 Frontend

| ID | Requirement | Source |
|---|---|---|
| FR-150 | Cancel button on the investigation page while non-terminal; confirm dialog; shows the resulting status. Two task rows with attempt number and status reason | Demo |

## 7. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-100 | No investigation remains non-terminal more than 10 min + one poll interval after creation (chaos-tested) |
| NFR-101 | A task timeout is visible in the timeline within one poll interval (≤ 3 s with jitter) |
| NFR-102 | Cancel returns within 2.5 s even when the Context Service or an agent hangs: step 2 has a 1 s total budget, step 3 a 1 s total budget (agents in parallel), SQLite steps are local. A failed close leaves `context_closed = 0` for the reconciler |
| NFR-103 | Both agents run concurrently; the investigation wall time ≈ the slower agent, not the sum |
| NFR-104 | The refactor (FR-100) adds no behaviour change measurable by the S2 suite |
| NFR-105 | The in-process chaos suite runs in < 60 s in CI (controllable clock, no real sleeps beyond 100 ms) |

## 8. Contracts

### 8.1 Deltas (WP-A1, land first; schema check must pass)

| Change | File | Note |
|---|---|---|
| `ErrorCode.INTERNAL_ERROR = "internal_error"`, category `lifecycle`, not re-dispatchable | `api/errors.py`, `a2a/status_mapping.py` | S2 N5 |
| `InvestigationCancelResponse {id, status, context_closed}` | `api/investigations.py` | FR-125 |
| `TaskCancelResponse {task_id, status: AgentTaskStatus}` | `a2a/task.py` | FR-102 |
| `GeointLlmOutput` lives in `nexus_geoint/schemas.py` (not contracts), like SIGINT's | — | Agent-private |
| `task.status_changed` payload: add `attempt` changes and `deadline` (when a re-dispatch sets a fresh one) | `events/timeline.py` | Timeline shows re-dispatch |
| `ErrorCode.INVESTIGATION_TERMINAL = "investigation_terminal"` (no generic `conflict` exists; checked) | `api/errors.py` | FR-125, G6 |
| `ErrorCode.FORBIDDEN = "forbidden"` (does not exist yet; checked) | `api/errors.py`; Context Service error mapping | FR-131 |

### 8.2 `GeointLlmOutput`

Same shape rules as `SigintLlmOutput` (S2 §8.2): every field required, `additionalProperties:false`, `classifications[]` of `{candidate_id, pattern_label, confidence, statement, reasoning_summary}`.

### 8.3 Orchestrator SQLite

No new tables or columns. `tasks.attempt`, `deadline`, `token_reservation`, `updated_at` and `acked_at` already exist; the re-dispatch wait window is derived (FR-121). Add an index on `tasks(status, deadline)` for the watcher. `investigations.context_closed` exists.

## 9. Workflow

```text
create → CREATED → RUNNING
          ├─ SIGINT  PENDING → DISPATCHED → RUNNING → COMPLETED ─┐
          └─ GEOINT  PENDING → DISPATCHED → RUNNING → TIMED_OUT  │
                       └─(agent ready)→ PENDING(attempt 2) → … → COMPLETED / terminal
all specialists terminal ──► terminal policy (FR-124) ─► PARTIAL / FAILED
deadline (10 min) ──► cancel open tasks ─► PARTIAL / FAILED (investigation_deadline)
cancel ──► CANCELLED ─► CS close (fence) ─► agent cancels ─► tasks CANCELLED
```

Every arrow is one SQLite transaction plus one timeline row, version-guarded (ADR-008 F4). A writer that loses a race re-reads and drops its write with `transition.dropped` if the edge is no longer legal.

## 10. Security and privacy

- `geoint-agent` mounts only `fixtures/geoint` and `data/replay/geoint-agent` (AC5 lint extended).
- Agent cancel accepts only the orchestrator token.
- GEOINT stored summaries follow the S2 N11 rule. The `sensor` field and `summary` free text never reach the graph.
- The `ev-geo-005` injection ("relocate to LOC-GRID-00", tripwire G2L) must not move any assertion: graph facts come from the detector only.

## 11. Model

Same pinned snapshot as SIGINT (`gpt-5.4-mini-2026-03-17`, `OPENAI_MODEL_EXTRACT`). No new model decision.

## 12. Failure handling

| Failure | Detection | Outcome | Test |
|---|---|---|---|
| Agent container stopped before ack | dispatch connect error | task stays PENDING, retried each tick until its deadline → TIMED_OUT → re-dispatch once if ready → else terminal; investigation PARTIAL/FAILED | chaos (both layers) |
| Agent stopped mid-task | polls fail (no change) until deadline | TIMED_OUT → re-dispatch when ready | chaos |
| Agent restarted mid-task | poll returns FAILED(agent_restarted) | re-dispatch attempt 2 | chaos |
| Agent lost the task (volume wiped) | poll 404 after ack | FAILED(task_lost) → re-dispatch | in-process |
| Attempt 1 slow, attempt 2 started | both may try to commit | at most one commit (batch key); the other gets 200 replay or 409 already_committed → reports committed ids | in-process |
| Orchestrator killed mid-run | — | resume sweep: re-POST PENDING (same attempt: safe duplicate), restart pollers, apply expired deadlines | chaos (containers) |
| CS down during cancel | close fails | `context_closed = 0`; reconciler closes within 30 s of CS returning | chaos (containers) |
| Agent ignores cancel and commits after the fence | CS 409 investigation_cancelled | attempt CANCELED, nothing committed | in-process |
| Investigation deadline | watcher | cancel open tasks → PARTIAL/FAILED(investigation_deadline) | in-process (clock) |
| Internal agent bug | exception | FAILED(internal_error), not re-dispatched | unit |

## 13. Observability

- One span per state transition (ADR-008 F9). New span names: `workflow.timeout`, `workflow.redispatch`, `workflow.cancel`, `workflow.reconcile_close`, `workflow.deadline`.
- Log fields: `investigation_id`, `task_id`, `attempt`, `agent_id`, `reason`. No content.
- AC3 trace: one trace contains both agents' spans under the investigation root. Verify with an in-memory exporter in the integration test. The Tempo-based proof stays in S6.

## 14. Acceptance criteria

| AC | Given | When | Then |
|---|---|---|---|
| AC3-a | stack in replay mode, `baseline-corroboration` | an investigation is created | two tasks (sigint, geoint) are dispatched concurrently; both COMPLETED; the investigation is `PARTIAL(agent_not_enabled)` with `skipped_agents=[analysis-agent]` |
| AC3-b | in-memory span exporter | the same run in-process | one trace id spans orchestrator dispatch, both agents' task spans and both Context Service batch spans |
| AC4-c | contract suite | GEOINT submit/poll/cancel examples | responses validate; duplicate/conflict/stale return 200/409/409; cancel is idempotent |
| AC4-d | replay, 3 scenarios | runs complete | GEOINT findings equal the oracle's geoint subset exactly (including `[ENT-CHARLIE, ENT-FOXTROT, LOC-GRID-09]` in `prompt-injection`), no tripwires |
| AC12-a | `make chaos`, geoint container stopped | an investigation is created | GEOINT → TIMED_OUT within deadline + 3 s, visible in the timeline; no re-dispatch while not ready; investigation `PARTIAL(deadline_exceeded)` with SIGINT findings kept |
| AC12-b | in-process, fake agent never answers | clock advances past 180 s | TIMED_OUT, attempt-2 dispatched with a fresh deadline and reduced budget; a second timeout is terminal |
| AC13-a | `make chaos`, geoint restarted mid-task | poll sees FAILED(agent_restarted) | attempt 2 dispatched and COMPLETED; investigation ends as in AC3-a |
| AC13-b | `make chaos`, orchestrator killed after dispatch | orchestrator restarts | resume sweep continues the run to the same terminal status; no duplicate findings; one trace (links) |
| AC13-c | `make chaos`, CS stopped, then cancel | CS comes back | investigation CANCELLED immediately; `context_closed` becomes 1 within 30 s; later agent commits get 409 |
| AC13-d | in-process | investigation deadline passes with one specialist COMPLETED | open task CANCELLED, investigation `PARTIAL(investigation_deadline)` |
| Carry-over | unit/integration | N5, N6, N7 cases | internal_error not re-dispatched; same-attempt re-POST with a new deadline is a 200 duplicate; cross-caller replay is 403 with no ids |

## 15. Test plan

| Layer | Tests |
|---|---|
| Unit (already prototyped) | The builder check ran FR-114–116 over the real fixtures: `baseline-corroboration` and `contradiction` match their oracles exactly, no noise slips through, `prompt-injection` gives zero patterns today (hence G2) |
| Unit | GEOINT: alias resolution, grid snapping (edges, off-grid), co-location (same/neighbour/diagonal/2-cells-away), conflict, cluster grouping, oracle shapes for all 3 scenarios, noise excluded. Orchestrator: watcher, re-dispatch rules (attempt cap, readiness, budget maths, fresh deadline), terminal policy table, cancel ordering, reconciler |
| Contract | cancel endpoints; `INTERNAL_ERROR` mapping; schema drift |
| Integration | runtime refactor: the full S2 suite unchanged; orchestrator + 2 fake agents via MockTransport with a controllable clock (in-process chaos, item 13); CS caller-scoped replay |
| E2E | compose, replay, 3 scenarios, both agents; oracle subsets; tripwires absent |
| Chaos | `make chaos` (item 14): AC12-a, AC13-a, AC13-b, AC13-c. Replay mode, no OpenAI key needed |

## 16. Implementation tasks and work packages

Two halves, each shippable and reviewable on its own:

- **S3a GEOINT** (AC3, AC4): WP-A1, WP-R, WP-G, WP-S, WP-E(part).
- **S3b failure paths** (AC12, AC13): WP-O, WP-X, WP-E(rest), WP-U.

### 16.1 Dependencies

```
WP-A1 contracts ─┬─> WP-R runtime extract ─> WP-G geoint ─> WP-S scenario+recordings ─┐
                 │                                                                    ├─> S3a E2E
                 │                                         WP-E compose (geoint) ─────┘
                 └─> WP-O orchestrator failure paths ─┬─> WP-X chaos (in-process, make chaos) ─> S3b
                     (+ CS FR-130/131)                └─> WP-U UI cancel
```
WP-R must land before WP-G (GEOINT is built on the runtime) and before WP-O's agent-cancel integration. WP-O can start on the orchestrator side in parallel with WP-R.

### 16.2 Packages

| WP | Owns | Tasks |
|---|---|---|
| **WP-A1 Contracts** | `contracts/src/nexus_contracts/{api/errors.py, a2a/status_mapping.py, a2a/task.py, api/investigations.py, events/timeline.py}`, `contracts/schemas/` | §8.1 deltas, schema regen, unit tests |
| **WP-R Runtime** | `shared/src/nexus_shared/agent/**` (new), `shared/pyproject.toml` (`[agent]` extra), `nodes/sigint-node/src/nexus_sigint/**` (shrinks), sigint tests (import paths) | FR-100–106; cancel endpoint and runner hooks |
| **WP-G GEOINT** | `nodes/geoint-node/**` | FR-110–119; prompt `geoint-classify@1.0.0` |
| **WP-S Scenario** | `data/scenarios/prompt-injection/**`, `data/fixtures/geoint/prompt-injection/**`, `scripts/record_replay.py`, `data/replay/geoint-agent/**`, `data/replay/sigint-agent/contradiction/**` | FR-140–142; S2 replay-key check |
| **WP-O Orchestrator + CS** | `apps/orchestrator/src/nexus_orchestrator/{workflow/**, delegation/**, api/routes.py, state/repository.py, settings.py}`, `context-service/src/nexus_context_service/{settings.py, repository.py}` | FR-120–131 |
| **WP-X Chaos** | `apps/orchestrator/tests/chaos/**` (new), `scripts/chaos.sh` (new), `Makefile` (`chaos` target), `infrastructure/docker/compose.chaos.yaml` (new), `shared/src/nexus_shared/llm/replay.py` (latency knob only) | items 13, 14, 14a |
| **WP-E Compose** | `infrastructure/docker/compose.yaml`, `.env.example`, `scripts/gen_secrets.py`, `scripts/check_ready.sh`, `tests/infra/test_compose_isolation.py` | geoint service, token, volume, mounts, AC5 lint |
| **WP-U UI** | `apps/frontend/src/**` (investigation page), e2e spec | FR-150 |

### 16.3 Ordered checklist
1. WP-A1 merged; schema check green.
2. WP-R merged; the full S2 suite is green, and the S2 replay E2E still passes in compose.
3. WP-G + WP-S: unit tests against all 3 oracles; `gen_scenario.py --check` green; S2 replay keys unchanged (or re-recorded); live recordings for GEOINT committed.
4. WP-E: stack up with both agents; S3a E2E (AC3-a/b, AC4-c/d). **Ship S3a.**
5. WP-O, WP-U merged behind unit + in-process chaos tests.
6. `make chaos` green locally (AC12-a, AC13-a/b/c); output pasted in the PR. **Ship S3b.**

## 17. Gaps and contradictions

Each gap lists options with the recommended one first.

| # | Gap | Options | Recommendation and tradeoff |
|---|---|---|---|
| G1 | **What GEOINT reports.** Noise contains 22–34 sightings of known people per scenario; the oracle (exact set, S2 G8) expects 1–3 | (a) Patterns only: co-location + conflict. (b) Every resolved sighting; GEOINT oracle becomes "contains". (c) Regenerate noise with unresolved subjects only | **Decided (2026-11-25): (a).** Matches baseline and contradiction exactly; keeps exact matching so an injected placement is caught; fixes `prompt-injection` with G2 |
| G2 | **`prompt-injection` FOXTROT is a lone sighting** | (a) Add a co-located CHARLIE sighting. (b) Drop the GEOINT expectation | **Decided: (a).** CHARLIE because any other partner (even an unresolved one) creates a new hidden path CHARLIE→FOXTROT→X; the window 14:30–15:30 avoids the ~19:00 contacts, so no corroboration |
| G3 | **`status_reason` when several specialists fail differently** | (a) First non-completed task by template order. (b) A new `multiple` code. (c) Most severe by a ranking | **(a).** Deterministic, no contract change; the task rows show each reason |
| G4 | **Budget check with analysis disabled** | (a) Compute and log only; S4 enables it. (b) Enforce now: a run that leaves < 8000 tokens ends PARTIAL(budget_exceeded) | **(a).** Enforcing it now would turn healthy runs into `budget_exceeded` for an agent that doesn't exist |
| G5 | **Same-attempt re-POST after a resume** recomputes nothing today, but S2 hashed `deadline` | (a) Exclude `deadline` from the agent `request_hash`. (b) Resume must resend the stored deadline | **(a)**, and resend the stored one anyway. Belt and braces: a deadline is scheduling metadata, not task content |
| G6 | **Cancel on a terminal investigation** | (a) 409 with the current status. (b) 200 no-op | **(a).** Tells the UI the truth; the button is hidden for terminal states anyway |
| G7 | **Re-dispatch when the agent stays down** | (a) Wait for readiness up to 180 s from the moment the task became retry-eligible (capped by the investigation deadline); then final. (b) One readiness check, then final. (c) Re-dispatch blindly | **(a)**, my call after the builder check. (b) gives an agent that restarts in 20 s no retry at all; (c) burns the single retry. Cost: a stopped agent now takes ≈ 2 × deadline to give up (AC12-a ≈ 360 s at defaults, so the chaos stack shortens the deadline) |
| G11 | **Cancel: background or in-request** | (a) In-request, fence then agents, each with a 1 s budget; reconciler covers failures. (b) Return after SQLite, do the network steps in the background | **(a)**, my call. `context_closed` in the response is then true information and AC13-c can assert it; (b) makes the field meaningless |
| G13 | **LocalStack (requested)**: no Nexus component uses object storage today; evidence is SQLite plus fixture files on read-only mounts | (a) Park as item 22: its own small slice after the MVP, with an `ObjectStore` seam and filesystem default. (b) Must in S3: fixtures/recordings from a LocalStack bucket now. (c) Add only the LocalStack container now, unused | **(a)**. It touches no S3 exit AC and adds a container, credentials and an egress/mount story to a slice that is already 5–6 d. (c) is the worst: an unused dependency. Promote to (b) only if a concrete consumer is named |
| G12 | **Deadline vs late agent result** (5 s grace race, resume after outage) | (a) Agent `FAILED(deadline_exceeded)` = `TIMED_OUT`; on resume, poll before applying deadlines. (b) Whichever the orchestrator records first wins | **(a)**, my call. (b) makes retry eligibility depend on scheduler jitter and breaks AC13-b's "same terminal status" |
| G8 | **Where chaos-container tests run** | (a) Local `make chaos` only; output in the PR. (b) CI with Docker | **(a)** for S3 (decision 2). (b) is item 21 |
| G9 | **GEOINT label set** | (a) `CO_LOCATION, DWELL_OVERLAP, CONFLICTING_PLACEMENT, INCONCLUSIVE`. (b) Free text | **(a).** Enumerated, testable; the gate still checks only kind/assertion/about (S2 G8), so labels are wording, not facts |
| G10 | **Refactor risk (FR-100)** | (a) Move-then-adapt with the S2 suite as the safety net, one WP. (b) Leave SIGINT alone and build the runtime fresh for GEOINT | **(a).** (b) recreates the duplication the decision was meant to avoid |

## 18. Undetermined (verify during implementation)

- ~~Whether noise regeneration changes SIGINT files~~: settled by the builder check. `gen_scenario.py` generates SIGINT noise before GEOINT and never reads GEOINT signal, so FR-140 cannot change a SIGINT fixture. The replay-key check in FR-141 stays as a cheap guard.
- ~~Generic `conflict` code~~: it doesn't exist; `investigation_terminal` and `forbidden` are added (§8.1).
- Whether `docker compose stop/kill/start` timing in `make chaos` is stable on macOS Docker Desktop within the 180 s deadline. Use a shorter `NEXUS_TASK_DEADLINE_SECONDS` override for the chaos stack if needed.
