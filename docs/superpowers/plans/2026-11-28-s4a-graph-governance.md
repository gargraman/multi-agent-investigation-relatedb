# S4a Graph Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Context Service filters every read by clearance with audited counts and retrieval receipts, accepts assessments with a server-side citation check and validated edges, serves I1–I4 and I6, retracts evidence with an impact set, and the orchestrator proxies all of it.

**Architecture:** One policy file drives operations and clearance. A `ReadScope` (caller rank, trusted-only flag, task id) is built per request and passed to every read; each read writes one audit event in its own lane transaction, with `RETURNED` edges for analysis reads (the receipt). New Cypher lives as versioned modules under `queries/`; repository methods stay "one lane transaction including its audit event".

**Tech Stack:** Python 3.12, FastAPI, Relate (embedded, Cypher 25), Pydantic v2 contracts with schema drift check, httpx ASGITransport tests, uv workspace.

**Spec:** `specs/02-features/S4a-graph-governance.md` (approved 2026-11-28). Parent: `specs/00-product/Nexus_MVP_Architecture_and_Design_Specification.md` v1.3 (§6.4, §6.5, §9, §10, §12.3, §14). Spike Cypher: `spikes/relate/queries.py`.

## Global Constraints

- Analysis read clearance `DEMO-MID` (rank 2); orchestrator `DEMO-HIGH` (rank 3); write clearance unchanged: sigint `DEMO-HIGH`, geoint `DEMO-MID` (spec §14.1, §9.5).
- Trusted = `status IN ['ACTIVE','DISPUTED']`. Analysis reads return trusted findings only; untrusted are not counted as filtered.
- Audit events record **counts** of hidden records (`filtered_above_clearance`), never their ids, statements or edges (NFR-202).
- Above-clearance object requested by id → **404**, never 403, with a `DENIED` audit event (§9.5).
- Header names: `X-Nexus-Task-Id` (analysis reads only), `X-Nexus-Actor` (orchestrator token only).
- Every mutating path and every filtered read is **one lane transaction including its audit event** (§14.4, NFR-203).
- No endpoint accepts Cypher; every query is a module constant under `context-service/src/nexus_context_service/queries/`.
- Error codes come from `nexus_contracts.api.errors.ErrorCode` (`unknown_finding_reference`, `already_committed`, `investigation_cancelled`, `schema_invalid`, `context_unavailable`).
- No model calls anywhere in S4a. Never run `make record-replay` or anything with `NEXUS_LLM_MODE=live`.
- Commits: Conventional Commits, author `gargraman <r.m.garg@gmail.com>`, trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Branch `feat/s4a-graph-governance`; never commit to `main`.
- Each task ends green on `uv run --all-packages pytest -q` and `make lint` (ruff check + format). Schema changes: `make schemas` then `make schemas-check`.

## Review Focus

1. **Idempotent replay after a retraction:** the same assessment body resubmitted after its cited finding became `NEEDS_REVIEW` must return 200 with the original response, not a citation error or a new write (idempotency is checked before citations). → Task 5.
2. **Cross-investigation citation:** a finding id from investigation B, retrieved by a task of investigation A's analysis in B, cited in A → 422 `unknown_finding_reference`. Receipts are scoped by investigation. → Task 5.
3. **Leak through the response body:** an analysis `/context` response must not contain a DEMO-HIGH finding's id anywhere (not in `about`, edges, or assessment). Assert by string search over the raw JSON. → Task 3.
4. **Retract evidence id from another investigation** (same manifest id, scoped `uid`) → only the addressed investigation changes. → Task 7.
5. **Orchestrator sends `X-Nexus-Task-Id`:** ignored, no 400, no receipt edges. → Task 3.

---

### Task 1: Contracts for S4a

**Files:**
- Create: `contracts/src/nexus_contracts/api/graph.py`
- Modify: `contracts/src/nexus_contracts/api/context.py` (add `SearchRequest`, `SearchResponse`, `InvestigationContext.assessment`)
- Modify: `contracts/src/nexus_contracts/events/timeline.py` (add `EVIDENCE_RETRACTED = "evidence.retracted"` and its payload)
- Modify: `contracts/src/nexus_contracts/registry.py` (register new schema models)
- Modify: `shared/src/nexus_shared/security.py` (add `TASK_ID_HEADER = "X-Nexus-Task-Id"`)
- Test: `contracts/tests/test_s4a_contracts.py`

**Interfaces:**
- Produces (all `ValueModel`):
  - `SearchRequest(investigation_id: InvestigationId, entity_ids: list[ManifestId] = [], kinds: list[FindingKind] = [], statuses: list[FindingStatus] = [], limit: int = Field(200, ge=1, le=500))`
  - `SearchResponse(findings: list[ContextFinding], truncated: bool)`
  - `InvestigationContext.assessment: Assessment | None = None`
  - `ProvenanceEvidence(evidence_id: ManifestId, sha256: str, owner_node_id: str, status: Literal["ACTIVE","RETRACTED"])`
  - `ProvenanceFinding(finding_id: FindingId, source_agent_id: str, status: FindingStatus, classification: Classification, evidence: list[ProvenanceEvidence])`
  - `Provenance(assessment_id: AssessmentId, findings: list[ProvenanceFinding])`
  - `ContradictionPair(finding_a: FindingId, finding_b: FindingId)` with validator `finding_a < finding_b`
  - `HiddenLinkWindow(cell_c: ManifestId, cell_b: ManifestId, from_: UtcDatetime = Field(alias="from"), until: UtcDatetime)`
  - `HiddenLink(entity_a: ManifestId, via: ManifestId, entity_b: ManifestId, sigint_findings: list[FindingId], geoint_findings: list[FindingId], windows: list[HiddenLinkWindow], disputed: bool)`
  - `GraphNodeLabel` StrEnum: `Investigation, Entity, Evidence, Finding, Assertion, Assessment, Decision`
  - `GraphNode(id: str, label: GraphNodeLabel, props: dict[str, str | float | int | bool | None])`
  - `GraphEdge(source: str, target: str, type: str)`
  - `Graph(nodes: list[GraphNode], edges: list[GraphEdge])`
  - `EvidenceRetractedPayload(evidence_id: ManifestId, decision_id: str, findings: int, assertions: int, assessments: int)`
  - `TASK_ID_HEADER` in `nexus_shared.security`

- [ ] **Step 1: Write the failing tests** in `contracts/tests/test_s4a_contracts.py`:
  - `test_search_request_defaults`: `SearchRequest(investigation_id=<valid>)` → `limit == 200`, lists empty; `limit=0` and `limit=501` raise.
  - `test_contradiction_pair_is_ordered`: `ContradictionPair(finding_a="fnd_b", finding_b="fnd_a")` raises.
  - `test_hidden_link_window_round_trips_from_alias`: `model_validate({"cell_c":..., "cell_b":..., "from": "2026-01-01T11:00:00Z", "until": "...12:00:00Z"})` and `model_dump(by_alias=True)` has key `"from"`.
  - `test_context_assessment_defaults_none`: an existing `InvestigationContext` JSON without `assessment` validates, `.assessment is None`.
  - `test_timeline_has_evidence_retracted`: `TimelineEventType("evidence.retracted")`.
- [ ] **Step 2: Run** `uv run pytest contracts/tests/test_s4a_contracts.py -q` → FAIL (import errors).
- [ ] **Step 3: Implement** the models above; register `search-request`, `search-response`, `provenance`, `contradiction-pair`, `hidden-link`, `graph`, `evidence-retracted-payload` in `SCHEMA_MODELS`; follow the file's existing docstring style (spec refs).
- [ ] **Step 4: Run** `make schemas && make schemas-check && uv run pytest contracts -q` → PASS.
- [ ] **Step 5: Commit** `feat(contracts): S4a search, provenance, links, graph and retraction event models`.

---

### Task 2: Policy file drives operations and clearance

**Files:**
- Create: `context-service/src/nexus_context_service/config/policy.yaml` (package data; see ruling below)
- Create: `context-service/src/nexus_context_service/policy.py`
- Modify: `context-service/src/nexus_context_service/auth.py` (drop `POLICY` dict; use `Policy`)
- Modify: `context-service/src/nexus_context_service/validation.py` (drop `CLEARANCE` dict; write clearance from `Policy`)
- Modify: `context-service/src/nexus_context_service/settings.py` (`policy_path: Path | None = None`)
- Modify: `context-service/src/nexus_context_service/main.py` (load at startup into `app.state.policy`)
- Modify: `context-service/pyproject.toml` if needed so the YAML ships in the wheel
- Test: `context-service/tests/test_policy.py`

**Ruling:** spec FR-201 names `context-service/config/policy.yaml`, but the runtime image copies only `.venv` (`infrastructure/docker/python.Dockerfile`), so a file outside the package would not ship. The file lives as package data at `nexus_context_service/config/policy.yaml`, loaded with `importlib.resources`; `policy_path` overrides it (tests, ops).

**Interfaces:**
- Produces:
  - `class Policy` with `@classmethod load(cls, path: Path | None) -> Policy` (raises `PolicyError` on malformed YAML, unknown classification, unknown key, or an operation name not in `OPERATIONS`)
  - `Policy.allows(caller: str, operation: str) -> bool`
  - `Policy.read_rank(caller: str) -> int` (1–3; `0` for a caller without `max_classification`)
  - `Policy.write_clearance(caller: str) -> Classification | None`
  - `OPERATIONS: frozenset[str]` = the 13 names currently in `auth.POLICY`
- YAML content (exact):
  ```yaml
  callers:
    orchestrator:
      read: DEMO-HIGH
      operations: [investigations.create, investigations.close, investigations.context,
                   assessments.provenance, evidence.retract, entities.contradictions,
                   investigations.links, investigations.graph, audit.read]
    analysis-agent:
      read: DEMO-MID
      operations: [investigations.context, search, assessments.create]
    sigint-agent:
      write: DEMO-HIGH
      operations: [findings.submit, entities.create]
    geoint-agent:
      write: DEMO-MID
      operations: [findings.submit, entities.create]
  ```

- [ ] **Step 1: Write failing tests** in `test_policy.py`:
  - `test_shipped_policy_matches_spec`: `Policy.load(None)`: `allows("analysis-agent","investigations.context")`, `not allows("analysis-agent","investigations.links")`, `read_rank("analysis-agent") == 2`, `read_rank("orchestrator") == 3`, `write_clearance("geoint-agent") is Classification.DEMO_MID`.
  - `test_malformed_policy_fails` (parametrised: not YAML; `read: DEMO-ULTRA`; operation `"nope"`; unknown top-level key) → `PolicyError`.
  - `test_service_refuses_to_start_on_bad_policy`: app with `policy_path` to a bad file → startup raises (use the app factory as `conftest.app` does).
  - `test_analysis_can_read_context_now`: analysis token `GET /v1/investigations/{id}/context` with `X-Nexus-Task-Id` → not 403 (status 200 or 404 acceptable here; Task 3 asserts content).
- [ ] **Step 2: Run** `uv run pytest context-service/tests/test_policy.py -q` → FAIL.
- [ ] **Step 3: Implement.** `authorize()` reads `request.app.state.policy`. `validation.py` callers get the clearance from the policy passed in (keep its function signatures; add a `clearance: Classification` parameter where `CLEARANCE[...]` was read).
- [ ] **Step 4: Run** `uv run pytest context-service -q` → PASS (existing findings-batch clearance tests still green).
- [ ] **Step 5: Commit** `feat(context-service): policy.yaml drives the operation allowlist and clearance`.

---

### Task 3: Read scope, read audit and receipts; filtered `/context`

**Files:**
- Create: `context-service/src/nexus_context_service/scope.py`
- Modify: `context-service/src/nexus_context_service/repository.py` (`get_context`, new `_write_read_audit`)
- Modify: `context-service/src/nexus_context_service/queries/context.py` (rank/trust filters, assessment read)
- Create: `context-service/src/nexus_context_service/queries/audit.py` (read audit + `RETURNED` edges)
- Modify: `context-service/src/nexus_context_service/routes.py`
- Test: `context-service/tests/test_read_scope.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class ReadScope: audit: AuditContext; max_rank: int; trusted_only: bool; task_id: str | None`
  - `def read_scope(operation: str) -> Callable[[Request], Awaitable[ReadScope]]` (FastAPI dependency wrapping `authorize(operation)`): for `analysis-agent`, `trusted_only=True` and `X-Nexus-Task-Id` required (missing or not matching the `TaskId` pattern → 400 ProblemDetails `schema_invalid`); for other callers the header is ignored and `task_id=None`, `trusted_only=False`.
  - `GraphRepository.get_context(self, investigation_id: str, scope: ReadScope) -> InvestigationContext`
  - `_write_read_audit(conn, scope: ReadScope, *, operation: str, investigation_id: str, filtered: int, returned_ids: list[str], now: datetime) -> None`: one `AuditEvent` with `authz="ALLOWED"`, `outcome="FILTERED" if filtered else "SUCCESS"`, `filtered_above_clearance=filtered`, `task_id=scope.task_id`; when `scope.task_id` is set, one `(AuditEvent)-[:RETURNED]->(Finding)` per returned id. Later tasks (4, 10) call it.
  - `cq.ASSESSMENT` returns the latest non-`SUPERSEDED` assessment with `classification_rank <= $max_rank` (null until Task 5 writes any).
- Filtering: `filtered` = number of findings in the investigation with `classification_rank > $max_rank` (trusted filter applied first for analysis, so untrusted are never counted).

- [ ] **Step 1: Write failing tests** in `test_read_scope.py` (seed with `conftest.batch_body`: one DEMO-HIGH SIGINT finding, two DEMO-MID GEOINT findings, one finding then moved to `NEEDS_REVIEW` with a direct Cypher write via `conftest.read`'s lane):
  - `test_analysis_context_hides_high_and_counts_it` (AC18-a part 1): analysis read → the 2 trusted MID findings only; exactly one new `AuditEvent` with `outcome="FILTERED"`, `filtered_above_clearance=1`, `task_id` = header value.
  - `test_analysis_context_body_never_mentions_hidden_id` (Review Focus 3): the DEMO-HIGH finding id is not a substring of `resp.text`; the audit event's properties contain no such id.
  - `test_receipt_edges_match_response`: `RETURNED` targets == returned finding ids.
  - `test_untrusted_not_returned_not_counted` (Trust-a part 1): the `NEEDS_REVIEW` finding is absent and `filtered_above_clearance` is unaffected by it.
  - `test_analysis_read_requires_task_header`: missing → 400; `"bogus"` → 400; no audit event written.
  - `test_orchestrator_sees_everything_and_header_ignored` (Review Focus 5): orchestrator read with `X-Nexus-Task-Id` → all 4 findings, `outcome="SUCCESS"`, no `RETURNED` edges, `task_id` null.
  - `test_context_assessment_is_null_without_one`.
- [ ] **Step 2: Run** `uv run pytest context-service/tests/test_read_scope.py -q` → FAIL.
- [ ] **Step 3: Implement.** Query params `$max_rank`, `$trusted_only` on `FINDINGS`, `ABOUT`, `EVIDENCE`, `LINKED_TO`, `PLACED_AT`; the audit write happens inside the same `_tx` as the reads (one lane run).
- [ ] **Step 4: Run** `uv run pytest context-service apps/orchestrator -q` → PASS (the orchestrator `/findings` proxy still works).
- [ ] **Step 5: Commit** `feat(context-service): clearance-filtered context with read audit and retrieval receipts`.

---

### Task 4: `POST /v1/search`

**Files:**
- Create: `context-service/src/nexus_context_service/queries/search.py`
- Modify: `repository.py` (`search`), `routes.py`
- Test: `context-service/tests/test_search.py`

**Interfaces:**
- Consumes: `ReadScope`, `read_scope("search")`, `_write_read_audit` (Task 3); `SearchRequest`, `SearchResponse` (Task 1).
- Produces: `GraphRepository.search(self, body: SearchRequest, scope: ReadScope) -> SearchResponse`. Returns `ContextFinding` rows assembled exactly as `get_context` does (extract the shared row assembly into one helper rather than duplicating it). Ordered `created_at, id`; `truncated = matched > limit`. For analysis, `statuses` ∩ trusted (empty request list = trusted set).

- [ ] **Step 1: Failing tests:**
  - `test_search_filters_and_or`: `entity_ids=[X]` returns only findings `ABOUT` X; `kinds=["OBSERVATION"]`; combined filters are AND.
  - `test_search_hides_high_and_audits` (AC18-a part 2): FILTERED event with `filtered_above_clearance=1` (count of above-clearance findings *matching the other filters*), receipt edges for returned ids.
  - `test_search_analysis_cannot_ask_for_untrusted`: `statuses=["NEEDS_REVIEW"]` → empty list.
  - `test_search_truncates`: seed 3 matching, `limit=2` → 2 rows, `truncated=True`.
  - `test_search_unknown_investigation_404`.
- [ ] **Step 2: Run** `uv run pytest context-service/tests/test_search.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest context-service -q` → PASS.
- [ ] **Step 5: Commit** `feat(context-service): structured search with clearance filter and receipt`.

---

### Task 5: `POST /v1/assessments`: write, idempotency, citation check

**Files:**
- Create: `context-service/src/nexus_context_service/queries/assessments.py`
- Create: `context-service/src/nexus_context_service/assessments.py` (pure helpers)
- Modify: `repository.py` (`submit_assessment`), `routes.py`
- Test: `context-service/tests/test_assessments.py`

**Interfaces:**
- Consumes: `AssessmentSubmission`, `AssessmentSubmissionResponse` (S0 contracts), `submission_key` pattern, `AlreadyCommitted`/`InvestigationClosed` exceptions pattern.
- Produces:
  - `assessment_key(task_id: str) -> str` returning `f"assessment:{task_id}"`
  - `used_finding_ids(body: AssessmentSubmission) -> list[str]` (sorted union of every `conclusions[].supporting_finding_ids` and `contradictions[].finding_ids`)
  - `assessment_hash(body: AssessmentSubmission) -> str` (sha256 of the canonical JSON of the body **excluding `attempt`**)
  - `class CitationRejected(Exception)`
  - `GraphRepository.submit_assessment(self, body: AssessmentSubmission, audit: AuditContext) -> tuple[AssessmentSubmissionResponse, bool]` (bool = created)
  - Edge handling in this task: every `proposed_edges` entry is listed in `rejected_edges` with `schema_invalid` **as a placeholder**; Task 6 replaces it. (Keeps this task's diff to the write path.)
- Order inside the one transaction (this order is the decision):
  1. investigation exists (else 404) and `write_state` OPEN (else 409 `investigation_cancelled`);
  2. existing `Submission` with the key → same hash: return stored response, 200, write nothing; different hash: 409 `already_committed` with `assessment_id` (Review Focus 1: this runs **before** citations);
  3. receipt = finding ids `RETURNED` by `AuditEvent {caller:'analysis-agent', task_id: $task}` whose finding is `CONTAINS`ed by this investigation;
  4. every used id ∈ receipt and visible at rank 2 → else write only a `DENIED` audit event (no ids) and raise `CitationRejected` → 422 `unknown_finding_reference`;
  5. status = `NEEDS_REVIEW` if any used finding is not trusted now, else `ACTIVE` (FR-211a);
  6. create `Submission {idempotency_key, content_hash, response_json}`, `Assessment` (all `_AssessmentBody` fields, `task_id`, `attempt`, `status`, `classification` + `classification_rank` = max over used findings, `created_at`), `BELONGS_TO`, `USES_FINDING` per used id, audit event `CONCERNS` the Submission.
- Assessment id: `asm_` + ULID, via the existing `ids.py` helper style.

- [ ] **Step 1: Failing tests** (helper: retrieve via analysis `/context` with a task id, then submit):
  - `test_assessment_commits_with_uses_finding` (AC7-s part 2): 201; `USES_FINDING` targets == cited ids; status `ACTIVE`; `classification_rank` = max of cited.
  - `test_citing_unretrieved_finding_rejected` (AC7-s part 1): F3 exists, visible, not retrieved by this task → 422 `unknown_finding_reference`; no `Assessment`, no `Submission`; one `DENIED` audit event whose properties contain no finding id.
  - `test_citing_hidden_or_foreign_same_error`: DEMO-HIGH id and an id from another investigation → identical 422 body.
  - `test_cross_investigation_receipt_does_not_count` (Review Focus 2).
  - `test_same_body_replays_200_different_body_409` (attempt differs only → still 200).
  - `test_replay_after_retraction_returns_original` (Review Focus 1): commit, mark cited finding `NEEDS_REVIEW` directly, resubmit same body → 200, same `assessment_id`, nothing new written.
  - `test_retraction_race_writes_needs_review` (Trust-a part 2): retrieve F1, mark F1 `NEEDS_REVIEW`, submit → 201 with stored `status = "NEEDS_REVIEW"`.
  - `test_receipt_spans_attempts`: retrieval with the same task id but the body `attempt=2` → 201.
  - `test_closed_investigation_409`.
  - `test_context_returns_the_assessment`: `/context` (orchestrator) `assessment.assessment_id` == committed id.
- [ ] **Step 2: Run** `uv run pytest context-service/tests/test_assessments.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest context-service -q` → PASS.
- [ ] **Step 5: Commit** `feat(context-service): assessment submission with receipt-based citation check`.

---

### Task 6: Edge validation and contradiction statuses

**Files:**
- Modify: `context-service/src/nexus_context_service/assessments.py`, `queries/assessments.py`, `repository.py` (`submit_assessment`)
- Test: `context-service/tests/test_assessment_edges.py`

**Interfaces:**
- Consumes: Task 5 transaction (step 6 gains edge writes); receipt set from step 3.
- Produces: `validate_edge(edge: ProposedEdge, receipt: set[str], visible: dict[str, FindingFacts]) -> ErrorCode | None` where `FindingFacts(kind: FindingKind, status: FindingStatus)`; cycle check is a query inside the transaction (`EXISTS { (target)-[:DERIVED_FROM]->+(source) }` means adding source→target closes a cycle).
- Rules (exact): unknown/foreign/hidden/unretrieved endpoint → `unknown_finding_reference`; `DERIVED_FROM` with non-`INFERENCE` source or closing a cycle → `schema_invalid`; an existing edge (same type; `CORROBORATES`/`CONTRADICTS` compared undirected) → accepted, no second relationship. Each accepted `CONTRADICTS`: both endpoints `ACTIVE → DISPUTED`, `version + 1`; `NEEDS_REVIEW`/`SUPERSEDED`/`DISPUTED` unchanged. Edge relationships carry `assessment_id` and `created_at`.

- [ ] **Step 1: Failing tests:**
  - `test_contradicts_disputes_both_and_keeps_them` (AC9-s part 1): both `DISPUTED`, versions +1, both still stored.
  - `test_bad_edge_rejected_individually`: one good `CORROBORATES` + one with an unretrieved endpoint → 201, `accepted_edges` = [good], `rejected_edges` = [{bad, `unknown_finding_reference`}].
  - `test_derived_from_needs_inference_source` and `test_derived_from_cycle_rejected` (seed an `INFERENCE` finding through `batch_body(kind="INFERENCE")`; if the batch validator forbids that for an agent, seed by direct Cypher and note it in the test docstring).
  - `test_existing_edge_not_duplicated`: second assessment (other task) proposing the reverse `CONTRADICTS` → accepted, still one relationship.
  - `test_contradicts_leaves_needs_review_alone`.
- [ ] **Step 2: Run** `uv run pytest context-service/tests/test_assessment_edges.py -q` → FAIL.
- [ ] **Step 3: Implement** (remove Task 5's placeholder rejection).
- [ ] **Step 4: Run** `uv run pytest context-service -q` → PASS.
- [ ] **Step 5: Commit** `feat(context-service): validated assessment edges and DISPUTED on contradiction`.

---

### Task 7: I2 evidence retraction

**Files:**
- Create: `context-service/src/nexus_context_service/queries/retraction.py` (I2 from `spikes/relate/queries.py`, scoped to `$inv`)
- Modify: `repository.py` (`retract_evidence`), `routes.py`
- Test: `context-service/tests/test_retraction.py`

**Interfaces:**
- Consumes: `RetractEvidenceRequest`, `RetractionImpact` (S0), `AuditContext.actor` (already resolved from `X-Nexus-Actor` for the orchestrator token).
- Produces: `GraphRepository.retract_evidence(self, investigation_id: str, evidence_id: str, reason: str | None, audit: AuditContext) -> tuple[RetractionImpact, bool]` (bool = newly retracted). Evidence addressed by `uid = f"{investigation_id}:{evidence_id}"`. Already `RETRACTED` → return the impact stored on the existing `Decision` (`impact_json`), write nothing. Actor `"operator:unknown"` when `audit.actor` equals the caller (no header). `Decision {id: "dec_"+ULID, actor, reason, created_at, impact_json}`, `(Decision)-[:DISPOSES]->(Evidence)`, audit event `CONCERNS` the Decision. Findings → `NEEDS_REVIEW` from any status except `SUPERSEDED`; assessments likewise. Ids in the response sorted. The route returns **201** when newly retracted and **200** when it was already retracted (Task 11 relies on this to decide whether to append a timeline event).

- [ ] **Step 1: Failing tests:**
  - `test_retraction_cascades_any_depth` (AC16-s): seed F1 `SUPPORTED_BY` E, F2 (INFERENCE, direct Cypher) `DERIVED_FROM` F1, F3 `DERIVED_FROM` F2, an assessment using F3 → E `RETRACTED`; F1, F2, F3, the assessment `NEEDS_REVIEW`; response lists them and F1's assertion ids; `Decision.actor == "operator:demo"` when the header is sent with the orchestrator token.
  - `test_retraction_is_idempotent`: first call 201, second call → 200, same `decision_id` and lists, one `Decision` node.
  - `test_retraction_scoped_to_investigation` (Review Focus 4): two investigations with the same evidence manifest id; retract in A → B untouched.
  - `test_retraction_unknown_evidence_404`, `test_retraction_allowed_after_close`.
  - `test_actor_header_ignored_from_other_callers`: covered by policy (analysis can't call retract → 403 audited); assert 403.
- [ ] **Step 2: Run** `uv run pytest context-service/tests/test_retraction.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest context-service -q` → PASS.
- [ ] **Step 5: Commit** `feat(context-service): evidence retraction with decision and impact set (I2)`.

---

### Task 8: I1 provenance and I3 contradictions

**Files:**
- Create: `context-service/src/nexus_context_service/queries/provenance.py`, `queries/contradictions.py`
- Modify: `repository.py` (`provenance`, `contradictions`), `routes.py`
- Test: `context-service/tests/test_provenance_contradictions.py`

**Interfaces:**
- Consumes: `ReadScope` (orchestrator, rank 3), `Provenance`, `ContradictionPair` (Task 1).
- Produces: `GraphRepository.provenance(self, assessment_id: str, max_rank: int) -> Provenance` (raises `NotFound` → 404 for unknown or `classification_rank > max_rank`; for the hidden case the route also writes a `DENIED` audit event via `record_denial`); `GraphRepository.contradictions(self, investigation_id: str, entity_id: str, max_rank: int) -> list[ContradictionPair]` (each pair once, lower id first, both findings visible; unknown entity → 404). Neither writes a read audit (not in FR-204's list).

- [ ] **Step 1: Failing tests:**
  - `test_provenance_trail`: assessment using F1 (E1) and F2 (E2) → findings sorted by id with their evidence and statuses; after retracting E1, E1's `status == "RETRACTED"` in the trail.
  - `test_provenance_hidden_is_404_at_lower_rank` (repository-level, `max_rank=2`, G7) and `test_provenance_unknown_404` (HTTP).
  - `test_contradictions_pair_once` (AC9-s part 2): after an accepted `CONTRADICTS`, I3 for the shared entity → exactly one pair.
  - `test_contradictions_hide_high_at_lower_rank` (repository-level, `max_rank=2`).
- [ ] **Step 2: Run** `uv run pytest context-service/tests/test_provenance_contradictions.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest context-service -q` → PASS.
- [ ] **Step 5: Commit** `feat(context-service): provenance (I1) and contradiction neighbourhood (I3)`.

---

### Task 9: I4 hidden links

**Files:**
- Create: `context-service/src/nexus_context_service/queries/links.py` (ADR-007 I4 text, verbatim from `spikes/relate/queries.py`)
- Modify: `repository.py` (`links`), `routes.py`
- Test: `context-service/tests/test_links.py`

**Interfaces:**
- Produces: `GraphRepository.links(self, investigation_id: str, max_rank: int) -> list[HiddenLink]`, `$radius = 1.5 * Investigation.grid_pitch`. Unknown investigation → 404.
- Seed: port the spike's seeded graph (`spikes/relate/test_c6_queries.py` fixtures) into a test helper that goes through `findings:batch` where the validator allows, direct Cypher otherwise.

- [ ] **Step 1: Failing tests (I4-a):**
  - `test_links_returns_the_hidden_path`: exactly one `{entity_a:"A", via:"C", entity_b:"B"}` with its findings, one window, `disputed=False`.
  - Negatives, each `[]`: `test_links_gone_after_retraction` (retract the SIGINT evidence via Task 7), `test_links_gone_with_direct_contact`, `test_links_gone_with_manifest_relation`, `test_links_gone_when_far`.
  - `test_links_high_direct_contact_at_mid_rank_keeps_path` (repository-level, `max_rank=2`): path still returned, the DEMO-HIGH finding id not in the result.
  - `test_links_disputed_flag`.
- [ ] **Step 2: Run** `uv run pytest context-service/tests/test_links.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest context-service -q` → PASS.
- [ ] **Step 5: Commit** `feat(context-service): cross-agent hidden links (I4)`.

---

### Task 10: I6 graph

**Files:**
- Create: `context-service/src/nexus_context_service/queries/graph.py`
- Modify: `repository.py` (`graph`), `routes.py`
- Test: `context-service/tests/test_graph.py`

**Interfaces:**
- Consumes: `read_scope("investigations.graph")`, `_write_read_audit` (Task 3, `returned_ids=[]` since only analysis gets receipts), `Graph` (Task 1).
- Produces: `GraphRepository.graph(self, investigation_id: str, scope: ReadScope) -> Graph`. Node `id`: `id` property, or `uid` for Entity/Evidence. Props whitelist per label (exact):
  - Investigation: `status`, `scenario_id`; Entity: `manifest_id`, `type`, `status`; Evidence: `manifest_id`, `classification`, `status`;
  - Finding: `kind`, `status`, `statement`, `confidence_band`, `classification`, `source_agent_id`; Assertion: `type`, `valid_from`, `valid_until`, `link_type`;
  - Assessment: `status`, `summary`, `classification`; Decision: `actor`, `created_at`.
  - Edges: every relationship between returned nodes except to/from `AuditEvent` and `Submission`.
  Findings/assessments above `scope.max_rank` are dropped with their assertions and edges and counted in `filtered`.

- [ ] **Step 1: Failing tests:**
  - `test_graph_shape`: seeded investigation → node labels ⊆ the 7, no `AuditEvent`/`Submission`, every edge endpoint is a returned node, Finding props keys == whitelist.
  - `test_graph_filters_at_lower_rank` (repository-level `ReadScope(max_rank=2)`): DEMO-HIGH finding and its assertion absent; audit event `FILTERED` with the count.
  - `test_graph_orchestrator_audit_success`: HTTP read → one `SUCCESS` audit event.
- [ ] **Step 2: Run** `uv run pytest context-service/tests/test_graph.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest context-service -q` → PASS.
- [ ] **Step 5: Commit** `feat(context-service): knowledge graph view (I6)`.

---

### Task 11: Orchestrator proxies and retraction timeline event

**Files:**
- Modify: `apps/orchestrator/src/nexus_orchestrator/delegation/context_client.py` (one method per proxied call)
- Modify: `apps/orchestrator/src/nexus_orchestrator/api/routes.py`
- Modify: the orchestrator timeline append path used for other events (`state/repository.py`)
- Modify: the Context Service double used by orchestrator tests (`apps/orchestrator/tests/orch_testkit.py` `FakeContextService`)
- Test: `apps/orchestrator/tests/test_graph_proxies.py`

**Interfaces:**
- Consumes: Task 1 models; `ACTOR_HEADER`.
- Produces routes (exact): `GET /api/v1/investigations/{id}/graph` → `Graph`; `GET /api/v1/assessments/{id}/provenance` → `Provenance`; `POST /api/v1/investigations/{id}/evidence/{evidence_id}/retract` → `RetractionImpact` (sends `X-Nexus-Actor: operator:demo`); `GET /api/v1/investigations/{id}/entities/{entity_id}/contradictions` → `list[ContradictionPair]`; `GET /api/v1/investigations/{id}/links` → `list[HiddenLink]`; `GET /api/v1/investigations/{id}/assessment` → `Assessment` (from `/context`'s `assessment`; 404 when null).
- Error mapping (same as the existing `get_findings`): Context Service 404 → 404; unreachable/5xx → 502 `context_unavailable`; other 4xx passed through with its detail.
- A Context Service **201** (newly retracted, Task 7) appends one `evidence.retracted` timeline event with `EvidenceRetractedPayload` and the proxy returns 201; a **200** (already retracted) appends nothing and returns 200.

- [ ] **Step 1: Failing tests:**
  - `test_each_proxy_passes_through` (parametrised over the 6 routes) against `FakeContextService` canned responses.
  - `test_retract_sends_operator_actor_and_appends_timeline` (Proxy-a): the fake records `X-Nexus-Actor == "operator:demo"`; timeline has one `evidence.retracted` with counts.
  - `test_retract_again_no_second_event`.
  - `test_assessment_404_when_none`, `test_context_down_is_502`.
- [ ] **Step 2: Run** `uv run pytest apps/orchestrator/tests/test_graph_proxies.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run --all-packages pytest -q && make lint` → PASS.
- [ ] **Step 5: Commit** `feat(orchestrator): proxies for I1–I4, I6, retraction and assessment`.
