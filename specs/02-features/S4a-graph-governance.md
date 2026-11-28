# S4a — Graph governance and graph-value queries (Context Service)

> **Status: Approved (2026-11-28).**

| Field | Value |
|---|---|
| Slice | S4 first half (spec §18 S4, 6–7 d for the whole slice; S4a ≈ 3–4 d) |
| Branch | `feat/s4a-graph-governance` |
| Exit ACs | AC18 (full); AC7, AC9 and AC16 server side (S4b adds the agent; S5 the Playwright half) |
| Template | spec §19.3 |
| Governing ADRs | 002 (audit in the same store), 003 (B6 assessment submission), 005 (D4 actor header), 007 (G1–G11, I4) |
| Builds on | `specs/02-features/S3-geoint-failure-paths.md`; S0 contracts `api/assessments.py`, `api/retraction.py`; spike `spikes/relate/queries.py` (I2, I4 verbatim) |

Spec references are to `specs/00-product/Nexus_MVP_Architecture_and_Design_Specification.md` v1.3. "verify" marks a claim about a library that was not checked.

---

## 0. Headline

The Context Service becomes the knowledge-governance layer the spec describes (§6.5), with no LLM involved:
- every read is filtered by the caller's clearance, and hidden records are counted in an audit event, never named (§9.5);
- an assessment can be submitted in one transaction with its validated edges, and contradictions move findings to `DISPUTED` (§9.4);
- the graph-value queries I1–I4 and I6 run as domain endpoints (§10), and evidence retraction returns its impact set (I2);
- the orchestrator exposes all of them at `/api/v1/...`, so S5 only draws.

S4b then adds the Analysis agent, the orchestrator's `ANALYZING` step, the `retraction` and `over-classified` scenarios and their recordings. Nothing in S4a calls a model; tests seed findings through `findings:batch` and submit hand-written assessments.

Decisions made before this draft (2026-11-28):
1. **S4 is split** into S4a (this spec: deterministic Context Service work) and S4b (agent + orchestrator + scenarios + recordings). The agent is then written against a built, tested contract, and recordings are not paid for twice.
2. **Retrieval receipt for the server-side ID validator.** The spec says an assessment may cite only findings "present in the retrieval result of that run" (§6.4) and that the check is "re-checked server-side" (§12.3). Analysis sends its `task_id` on every read; the audit event that read writes anyway records which findings it returned; `POST /v1/assessments` checks citations against everything that task retrieved. Rejected: recomputing visibility only (lets through a finding the agent never retrieved), a signed retrieval token (key handling for no gain).
3. **Analysis sees only trusted findings** (`ACTIVE`, `DISPUTED`). If a retraction lands between retrieval and submit, the assessment is still written, but as `NEEDS_REVIEW` (FR-211a): the paid LLM run is kept and no `ACTIVE` assessment rests on untrusted findings. Rejected: a 422 (throws the run away), blocking retraction during analysis (blocks the operator, misses earlier retractions).
4. **`DERIVED_FROM` depth is shown on seeded data only in P0.** No agent writes `INFERENCE` findings and Analysis conclusions stay inside the Assessment, so in a real run the I2 cascade is evidence → findings → assessments. The `<-[:DERIVED_FROM]-+` cascade is built and tested on seeded graphs. Rejected: materialising conclusions as findings (+≈1 d, contract and policy change), dropping the `INFERENCE`-source rule (meaningless observation-to-observation edges).

---

## 1. Must-have vs Good-to-have

Rule: Must only if AC7, AC9, AC16 or AC18 needs it server side, S4b or S5 is blocked without it, or the spec mandates it for P0.

| # | Feature | What | Impact | Effort / risk | M/N |
|---|---|---|---|---|---|
| 1 | Policy file | `context-service/config/policy.yaml`: operations and read clearance per caller, replacing the in-code `POLICY` and clearance tables. Analysis gets `investigations.context` | Spec §14.1; AC18 | S / low | Must |
| 2 | Read audit + receipt | Every clearance-filtered read writes one `AuditEvent` (`SUCCESS` or `FILTERED` with `filtered_above_clearance`); an analysis read carries `X-Nexus-Task-Id` and its audit event `RETURNED` each finding it returned | AC18; decision 2 | S / low | Must |
| 3 | `/context` filtered | Findings above the caller's clearance dropped and counted; the response gains `assessment` (see FR-215) | AC18 | S / low | Must |
| 4 | `POST /v1/search` | Structured filter (entity, kind, status, classification ≤ clearance), receipt as in 2 | Spec §12.3; S4b retrieval | S / low | Must |
| 5 | `POST /v1/assessments` | One transaction: `Submission` (key = task_id), `Assessment`, `USES_FINDING`, validated edges, `DISPUTED` statuses, audit. Citation check against the receipt | AC7, AC9 | M / med | Must |
| 6 | I1 provenance | `GET /v1/assessments/{id}/provenance` | Spec §10 | S / low | Must |
| 7 | I2 retraction | `POST /v1/investigations/{id}/evidence/{evidence_id}/retract`: `RETRACTED`, `Decision`, `NEEDS_REVIEW` at any depth, impact set, actor | AC16 | M / low (spike query exists) | Must |
| 8 | I3 contradictions | `GET /v1/investigations/{id}/entities/{entity_id}/contradictions` | AC9 | S / low | Must |
| 9 | I4 hidden links | `GET /v1/investigations/{id}/links`, ADR-007 query verbatim with `$max_rank` | Spec §10 litmus | S / low (spike passed) | Must |
| 10 | I6 graph | `GET /v1/investigations/{id}/graph`: nodes and edges, clearance filtered, `FILTERED` audit | AC19 later; S5 | S / low | Must |
| 11 | Orchestrator proxies | `/api/v1/...` rows of §12.1 for I1–I4, I6, retract, and `GET .../assessment` | S5 not blocked | S / low | Must |
| 12 | `GET /v1/audit/{object_id}` | Permitted audit history of one object | Demo, debugging | S / low | Nice |
| 13 | Assessment `SUPERSEDED` on attempt 2 | A second assessment for the same investigation supersedes the first | Only matters with re-dispatched analysis (S4b) | S / low | Nice (S4b decides) |

---

## 2. Context

- Context Service routes today: `/livez`, `/readyz`, `POST /v1/investigations`, `POST /v1/investigations/{id}/close`, `POST /v1/findings:batch`, `GET /v1/investigations/{id}/context` (orchestrator only, unfiltered).
- `auth.py` holds the operation allowlist as an in-code table and already lists every S4 operation (`search`, `assessments.create`, `assessments.provenance`, `evidence.retract`, `entities.contradictions`, `investigations.links`, `investigations.graph`, `audit.read`). `validation.py` holds the per-caller max classification and already enforces it on `findings:batch` writes.
- Every Finding stores `classification_rank` (1–3). Every mutating repository method is one lane transaction including its `AuditEvent` (`repository.py`).
- Contracts already exist from S0: `AssessmentSubmission`, `ProposedEdge`, `RejectedEdge`, `AssessmentSubmissionResponse`, `Assessment`, `RetractEvidenceRequest`, `RetractionImpact`, `InvestigationContext`, `ContextFinding`.
- The S0 spike ran I1, I2 (quantified path `<-[:DERIVED_FROM]-+`), I3 and I4 verbatim on Relate, including the clearance and retraction negatives (spike report row 6). The finished I2 and I4 text is in `spikes/relate/queries.py`.
- The orchestrator already proxies `GET /api/v1/investigations/{id}/findings` from `/context`, and has `X-Nexus-Actor` support in `nexus_shared.security`.
- The orchestrator's `ANALYZING` state and transitions exist but nothing enters them (S4b).

## 3. Goal

Everything the Analysis agent (S4b) and the UI (S5) need from the graph exists, is governed and is tested: reads are clearance-filtered and audited (AC18), an assessment can only cite what its run retrieved (AC7), a validated contradiction marks both findings `DISPUTED` (AC9), and retracting evidence flags every dependent finding and assessment at any depth (AC16).

## 4. Scope

**In:** items 1–11.
**Out:** the Analysis agent, prompt, aliases and agent-side validator (S4b); orchestrator `ANALYZING` and the terminal rule change (S4b); `retraction` and `over-classified` scenario packs and recordings (S4b); UI (S5); I5 graph-shaped retrieval and I7 (P1); items 12–13 unless time remains.

## 5. Actors and dependencies

| Actor | Role in S4a | Clearance (read) |
|---|---|---|
| orchestrator | all I-queries, retract (with operator actor), context, graph, audit | DEMO-HIGH (operator view) |
| analysis-agent | context, search, assessments create | DEMO-MID |
| sigint-agent, geoint-agent | unchanged (writes only) | n/a for reads |
| context-service | everything below | — |

Analysis has no running service in S4a: its token is generated by `make secrets` (already, since S1) and tests call with it.

---

## 6. Functional requirements

### 6.1 Policy and clearance

- **FR-201** `context-service/config/policy.yaml` holds, per caller: allowed operations and `max_classification` (read). Write clearance stays where it is (`validation.py`) but reads its numbers from the same file. Loaded once at startup; a malformed file fails startup. The in-code tables are removed. Shape:
  ```yaml
  callers:
    orchestrator:   {max_classification: DEMO-HIGH, operations: [investigations.create, ...]}
    analysis-agent: {max_classification: DEMO-MID,  operations: [investigations.context, search, assessments.create]}
  ```
- **FR-202** Policy content equals spec §14.1, plus `investigations.context` for `analysis-agent` (the S2 FR-044 deferral is lifted now that reads are filtered).
- **FR-203** "Visible to the caller" means: same investigation, and `classification_rank ≤` the caller's rank. For an Assessment, its own `classification_rank` (FR-212). "Trusted" means `status IN ['ACTIVE','DISPUTED']`. **For `analysis-agent`, every read also requires trusted** (decision 3): untrusted findings are simply not returned and are not counted as filtered (they are not hidden for clearance). The orchestrator sees every status.

### 6.2 Read audit and retrieval receipt

- **FR-204** `/context`, `/search` and `/graph` each write exactly one `AuditEvent` per successful call, in the read's own transaction: `operation`, `caller`, `target = investigation_id`, `outcome = FILTERED` with `filtered_above_clearance: <n>` when n > 0, else `SUCCESS`. Counts only: no id, statement or edge of a hidden record is written anywhere.
- **FR-205** A call by `analysis-agent` to `/context` or `/search` must carry `X-Nexus-Task-Id` (pattern `TaskId`); missing or malformed → 400. Its audit event stores `task_id` and has one `(AuditEvent)-[:RETURNED]->(Finding)` edge per finding in the response. The header is ignored from other callers.
- **FR-206** The receipt of a task is the set of findings `RETURNED` by every analysis audit event with that `task_id` in that investigation, across attempts (attempt 2 may cite what attempt 1 retrieved: same task, same investigation, same clearance).

### 6.3 Reads

- **FR-207** `GET /v1/investigations/{id}/context`: response as today, findings filtered by FR-203, ordered as today. Adds `assessment: Assessment | null`: the investigation's latest `ACTIVE`, `DISPUTED` or `NEEDS_REVIEW` assessment visible to the caller.
- **FR-208** `POST /v1/search` body `SearchRequest {investigation_id, entity_ids?: [ManifestId], kinds?: [FindingKind], statuses?: [FindingStatus], limit: 1..500 = 200}` → `SearchResponse {findings: [ContextFinding], truncated: bool}`. Filters are AND, each list is OR; `classification ≤ clearance` is always applied, and for analysis `statuses` is intersected with the trusted set. Unknown investigation → 404. `entity_ids` match through `ABOUT`.

### 6.4 Assessment submission (ADR-003 B6)

- **FR-209** `POST /v1/assessments` (analysis-agent only) takes `AssessmentSubmission` and writes in **one** transaction: a `Submission` (`idempotency_key = task_id`, `content_hash`), the `Assessment` (`status ACTIVE`), `(Assessment)-[:BELONGS_TO]->(Investigation)`, `(Assessment)-[:USES_FINDING]->(f)` for every id in any conclusion's `supporting_finding_ids` or any `contradictions[].finding_ids`, the accepted edges, status changes (FR-213), and one `AuditEvent` that `CONCERNS` the Submission.
- **FR-210** Idempotency as the findings batch (§8.5): same key and same hash → 200 with the original response; different hash → 409 `already_committed` with `assessment_id`. Closed investigation → 409 `investigation_cancelled`. Hash covers the body minus `attempt`.
- **FR-211** **Citation check (the server-side ID validator).** Every id the assessment *uses* (FR-209 set) must be in the task's receipt (FR-206) **and** still visible (FR-203). Any miss → the whole assessment is rejected: 422 `unknown_finding_reference`, nothing written except a `DENIED`-outcome audit event (no ids in it). A miss never distinguishes "does not exist", "other investigation", "above clearance" and "not retrieved" (§9.5).
- **FR-211a** **Retraction race.** If every used id passes FR-211 but any of them is no longer trusted at submit time, the assessment is written with `status NEEDS_REVIEW` instead of `ACTIVE` (everything else as FR-209). Edges are still validated per FR-213.
- **FR-212** `Assessment.classification_rank` = max rank of the findings it uses.
- **FR-213** **Edge validation, per edge** (rejected individually, listed in `rejected_edges`, the rest commits):
  - both findings in the receipt and visible, else `unknown_finding_reference`;
  - `DERIVED_FROM`: the source must be `kind INFERENCE` and the edge must not close a `DERIVED_FROM` cycle, else `schema_invalid`;
  - an edge already present (same type, same endpoints; `CORROBORATES`/`CONTRADICTS` compared undirected) is accepted without a second relationship.
  
  For each accepted `CONTRADICTS`, both findings move `ACTIVE → DISPUTED` (§9.4). `NEEDS_REVIEW` and `SUPERSEDED` are left as they are; `DISPUTED` stays `DISPUTED`. Each change bumps the finding's `version`.
- **FR-214** Response `AssessmentSubmissionResponse` (exists). 201 on first commit.

### 6.5 Graph-value queries

All are versioned Cypher in `context-service/src/nexus_context_service/queries/`, parameterised with `$inv` and `$max_rank` (the caller's rank), and trust a finding only while `status IN ['ACTIVE','DISPUTED']` where the spec says so.

- **FR-215** **I1** `GET /v1/assessments/{id}/provenance` → `Provenance {assessment_id, findings: [{finding_id, source_agent_id, status, classification, evidence: [{evidence_id, sha256, owner_node_id, status}]}]}`. Unknown or above-clearance assessment → 404.
- **FR-216** **I2** `POST /v1/investigations/{id}/evidence/{evidence_id}/retract` (body `RetractEvidenceRequest`, optional) in one transaction:
  - evidence `ACTIVE → RETRACTED`;
  - findings `SUPPORTED_BY` it, and every finding reaching them through `<-[:DERIVED_FROM]-+`, → `NEEDS_REVIEW` (from any status but `SUPERSEDED`);
  - every assessment `USES_FINDING` any of those → `NEEDS_REVIEW`;
  - one `Decision {id, actor, reason, created_at}` with `(Decision)-[:DISPOSES]->(Evidence)`, one `AuditEvent`;
  - returns `RetractionImpact` (exists), ids sorted.
  
  `actor` comes from `X-Nexus-Actor` with the orchestrator token only (ADR-005 D4); absent → `operator:unknown`. Already retracted → 200 with the original impact set and decision, nothing written. Unknown evidence or investigation → 404. Closed investigation: retraction is still allowed (it is an operator disposition, not an agent write).
- **FR-217** **I3** `GET /v1/investigations/{id}/entities/{entity_id}/contradictions` → `[{finding_a, finding_b}]`, each pair once (lower id first), both visible, from `CONTRADICTS` between findings both `ABOUT` the entity. Unknown entity → 404.
- **FR-218** **I4** `GET /v1/investigations/{id}/links` → `[HiddenLink {entity_a, via, entity_b, sigint_findings, geoint_findings, windows: [{cell_c, cell_b, from, until}], disputed}]`: the `spikes/relate/queries.py` I4 text with `$radius = 1.5 × grid.pitch` from `Investigation.grid_pitch` (already stored at create: `repository.py`, `queries/investigations.py`).
- **FR-219** **I6** `GET /v1/investigations/{id}/graph` → `Graph {nodes: [{id, label, props}], edges: [{source, target, type}]}`. Labels: Investigation, Entity, Evidence, Finding, Assertion, Assessment, Decision. No `AuditEvent`, `Submission`. Findings and assessments above clearance are dropped with their assertions and edges, and counted in the FR-204 audit event. `props` is a fixed whitelist per label (ids, status, kind, classification, type, statement, summary, confidence_band, valid_from/until); never evidence content (there is none) or prompts.

### 6.6 Orchestrator

- **FR-220** Proxies with the orchestrator token, Context Service errors passed through as ProblemDetails (502 `context_unavailable` when it is down): `GET /api/v1/investigations/{id}/graph`, `GET /api/v1/assessments/{id}/provenance`, `POST /api/v1/investigations/{id}/evidence/{evidence_id}/retract` (sends `X-Nexus-Actor: operator:<user>`; P0 user is `demo`), `GET /api/v1/investigations/{id}/entities/{entity_id}/contradictions`, `GET /api/v1/investigations/{id}/links`, `GET /api/v1/investigations/{id}/assessment` (from `/context`'s `assessment`; 404 when null).
- **FR-221** A retraction appends a timeline event `evidence.retracted` with the impact counts (not ids beyond the evidence id), so the UI timeline shows it.

## 7. Non-functional requirements

- **NFR-201** Every endpoint answers in < 500 ms p95 on the seeded scenarios on a dev laptop (demo scale; no performance claim beyond that).
- **NFR-202** No hidden record's id, statement or count per type appears in any response, log line, span attribute or audit event of a caller below its clearance; only the total count in the audit event.
- **NFR-203** Every new mutating path is one lane transaction with its audit event (spec §14.4).

## 8. Contracts

New in `nexus_contracts` (schema drift check must pass): `SearchRequest`, `SearchResponse`, `Provenance`, `ContradictionPair`, `HiddenLink`, `Graph` (+ node/edge), `TimelineEventType.EVIDENCE_RETRACTED`. Changed: `InvestigationContext.assessment`. Existing and used as is: `AssessmentSubmission`, `AssessmentSubmissionResponse`, `RejectedEdge`, `Assessment`, `RetractEvidenceRequest`, `RetractionImpact`. Header constant `TASK_ID_HEADER = "X-Nexus-Task-Id"` in `nexus_shared.security`.

Graph additions: `(AuditEvent)-[:RETURNED]->(Finding)`; `AuditEvent.task_id`, `AuditEvent.filtered_above_clearance`; `Assessment.classification_rank`; `Decision` node (§9.1).

## 9. Workflow

Analysis read → submit (S4b will drive it; S4a tests do it by hand):
1. `GET /context` (or `POST /search`) with analysis token + `X-Nexus-Task-Id` → filtered findings; audit event `RETURNED` them.
2. `POST /v1/assessments` → citation check against the receipt → one transaction → 201.
3. Operator, via orchestrator: I1/I3/I4/I6 reads; retract → impact set; timeline event.

## 10. Security and privacy

- 404, never 403, for an object above clearance requested by id (§9.5); `DENIED` audit event.
- `X-Nexus-Actor` only with the orchestrator token; `X-Nexus-Task-Id` only from analysis-agent.
- Audit events record counts of hidden records, never ids (NFR-202).
- No endpoint takes Cypher; every query is a versioned file.

## 11. Failure handling

| Case | Behaviour |
|---|---|
| Cited id not in receipt / not visible | 422 `unknown_finding_reference`, nothing written but a `DENIED` audit |
| Bad edge | rejected individually, rest commits |
| Same task resubmits same body | 200, original response |
| Same task, different body | 409 `already_committed` |
| Investigation closed | assessment 409 `investigation_cancelled`; reads and retract still work |
| Store unavailable | 503 (existing `_require_ready`) |
| Policy file malformed | service fails to start (readyz never green) |

## 12. Observability

Existing span per request; new attributes `nexus.audit.outcome`, `nexus.filtered_above_clearance` (count), `nexus.assessment.rejected_edges` (count). No ids of hidden records, no statements.

## 13. Acceptance criteria

- **AC18-a** Given a DEMO-HIGH SIGINT finding and DEMO-MID findings, when analysis reads `/context` and `/search`, then the DEMO-HIGH finding is absent and each call wrote a `FILTERED` audit event with `filtered_above_clearance ≥ 1` and no id of it.
- **AC7-s** Given analysis retrieved F1, F2 (not F3, which exists and is visible), when it submits an assessment citing F3, then 422 `unknown_finding_reference` and nothing is written; citing only F1, F2 → 201 with `USES_FINDING` to exactly those.
- **AC9-s** Given an accepted `CONTRADICTS` F1→F2, then both are `DISPUTED`, both remain stored, and I3 for their shared entity returns the pair.
- **Trust-a** Given F2 is `NEEDS_REVIEW`, analysis `/context` and `/search` do not return it (and do not count it as filtered). Given analysis retrieved F1 and F1's evidence is then retracted, the submitted assessment citing F1 is 201 with status `NEEDS_REVIEW`.
- **AC16-s** Given F2 `DERIVED_FROM` F1 `SUPPORTED_BY` E and an assessment using F2, when E is retracted, then E is `RETRACTED`, F1 and F2 and the assessment are `NEEDS_REVIEW`, a `Decision` with actor `operator:demo` exists, the response lists them, and I4 paths resting on F1 disappear.
- **I4-a** On a seeded baseline-corroboration graph, `/links` returns exactly the oracle's `{a, via, b}`; the ADR-007 negatives (retracted, direct link, manifest link, far cell) return `[]`; a DEMO-HIGH direct contact queried at DEMO-MID keeps the path and leaks nothing.
- **Proxy-a** Each FR-220 route returns the Context Service result through the orchestrator, and retract records the actor.

## 14. Test plan

- **Unit:** trusted filter for analysis vs orchestrator reads; policy loading (good, malformed, unknown caller), receipt set computation, edge validation rules incl. cycle, hash stability.
- **Integration (Context Service, real Relate):** every AC above; idempotency (same/different body); FILTERED counts; 404-not-403 paths; two investigations don't mix (receipts, I3, I4, retraction).
- **Contract:** schemas regenerated, drift check green.
- **Orchestrator:** proxy tests with the Context Service double; retract timeline event.
- No E2E or chaos changes in S4a.

## 15. Implementation tasks (ordered)

1. Contracts + header constant (land first).
2. `policy.yaml` + loader; auth and validation read it.
3. Read audit + receipt (FR-204–206) on `/context`; context filtering + `assessment` field.
4. `/search`.
5. `/assessments` with citation check and edge validation.
6. I2 retraction.
7. I1, I3.
8. I4.
9. I6.
10. Orchestrator proxies + timeline event.

## 16. Gaps and contradictions

| # | Gap | Resolution (my call unless marked) |
|---|---|---|
| G1 | §12.3 has no assessment read endpoint, yet §12.1 has `GET /api/v1/investigations/{id}/assessment` | Fold it into `/context` (FR-207) instead of a new endpoint |
| G2 | §9.5 says FILTERED "whenever count > 0" but receipts need an event on every analysis read | Every read writes one event; outcome `SUCCESS` when nothing was hidden (FR-204) |
| G3 | Which assessment statuses count as "current" for the read | Latest non-`SUPERSEDED`; superseding itself is item 13 |
| G4 | `contradictions[]` (prose) and `CONTRADICTS` edges are separate fields | Server derives nothing from `contradictions[]`; only edges change statuses. S4b's agent must emit both |
| G5 | Retraction of an already-retracted evidence | Idempotent: original impact, nothing new written |
| G6 | Every UI `/findings` poll now writes an audit node | Accepted at demo scale; revisit in S6 if the store grows noticeably |
| G7 | I1, I3, I4 and I6 are orchestrator-only, and the orchestrator reads at DEMO-HIGH, so their below-clearance branches (I4-a's DEMO-MID case, FR-219 drop-and-count, FR-215's 404) never fire over HTTP | No "view at clearance" parameter in P0 (YAGNI). The queries keep `$max_rank`, and those branches are tested at the repository level; I4-a's DEMO-MID case is a repository test. A demo toggle can add the parameter later (S5, Nice) |

## 17. Undetermined (verify during implementation)

- Relate behaviour for the `DERIVED_FROM` cycle check inside the write transaction (quantified path in a `WHERE EXISTS`).
