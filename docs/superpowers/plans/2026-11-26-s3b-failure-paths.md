# S3b Failure Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every investigation reach a clearly reported terminal state despite agent stops, restarts, lost tasks, orchestrator crashes, Context Service outages and user cancels (AC12, AC13). Close the S3a carry-overs first, starting with a runtime output-side guard on model text.

**Architecture:** The per-investigation driver (`Poller._drive`) gets a fixed tick order: deadlines (FR-120/123), then re-dispatch (FR-121), then dispatch PENDING, then poll, then finish (FR-124). Every write still goes through the version-guarded `transition_task` / `transition_investigation`. Cancel and the investigation deadline share one fence-first routine: SQLite terminal first, then CS close, then agent cancels, then tasks CANCELLED. A leader-only reconciler closes anything the fence missed. Time comes from an injected `Clock`, so the in-process chaos suite runs on a fake clock in under 60 s.

**Tech Stack:** Python 3.12, uv workspace, FastAPI, aiosqlite, httpx (`MockTransport` in tests), Pydantic v2, OpenTelemetry, pytest-asyncio, Docker Compose, React + TypeScript (`apps/frontend`), Playwright.

**Spec:** `specs/02-features/S3-geoint-failure-paths.md` (S3b half: WP-O, WP-X, WP-E rest, WP-U; FR-120–128, FR-150, NFR-100–105, AC12, AC13, §12, §17 G3/G4/G6/G7/G11/G12). S3a is merged locally to `main` at `4f77fd0`. Parked items come from `.superpowers/sdd/2026-11-25-s3a-geoint/progress.md`.

## Scope (Must vs Good-to-have)

| # | Task | What | Impact | Effort / risk | M/N |
|---|---|---|---|---|---|
| 1 | Output-side text guard | Reject fence markers, URLs, `NEXUS-` tokens and out-of-scope ids in statement/reasoning; the repair call, then SCHEMA_INVALID | Closes the final-review Important; S4 prerequisite | S / med (could invalidate committed recordings) | Must |
| 2 | Runtime hardening | Endpoint tests (SUBMITTED cancel, REJECTED supersede); the CANCELED-write crash path; move `band_for` to shared; test fixes | Locks the agent behaviour FR-121 depends on | S / low | Must |
| 3 | Orchestrator foundations | Clock, settings, index, TaskRecord fields, event deadline, attempt guard | Everything below | S / low | Must |
| 4 | Delegation clients | Agent readiness and cancel; CS close | FR-120/121/125/126 | S / low | Must |
| 5 | Terminal policy | FR-124 + FR-121a | AC13 | S / low | Must |
| 6 | Deadline watcher | FR-120 + FR-121b | AC12 | S / low | Must |
| 7 | Re-dispatch | FR-121/122, dispatch PENDING every tick | AC13 | M / med (races) | Must |
| 8 | Fence routine + investigation deadline | FR-123 | AC13-d, NFR-100 | M / med | Must |
| 9 | Cancel endpoint | FR-125 | Spec §8.6, AC13-c | S / med | Must |
| 10 | Close reconciler | FR-126 | AC13-c | S / low | Must |
| 11 | Resume sweep + budget | FR-127, FR-128 | AC13-b | S / med | Must |
| 12 | In-process chaos | §12 rows, AC12-b, AC13-d, < 60 s | AC12/13 in CI | M / low | Must |
| 13 | `make chaos` | Replay latency knob, compose.chaos.yaml, chaos.sh | AC12-a, AC13-a/b/c | M / med (timing) | Must |
| 14 | UI cancel + task rows | FR-150, TS types for the cancel models | Demo | S / low | Must |
| 15 | E2E hardening + S3b verification | Fix S3a E2E minors, screen summaries for tripwires, full verification | Ship gate | S / low | Must |
| 16 | CS cross-caller hardening | Audit DENIED row, two untested paths, generic 403 detail | Audit completeness | S / low | Good-to-have |
| 17 | Oracle-free agent manifests | Generated manifest projection; agents mount that instead of `data/scenarios` | Removes oracle exposure to agents | M / low | Good-to-have |

Parked and not in this plan, because they are cosmetic or carry no behaviour risk:
- warning noise on a superseded commit;
- the short-name helper duplication;
- the `ClassifyResult` generic;
- test helpers that live in a test module;
- the SIGINT ISOLATED_CONTACT label (demo note only);
- the ~120-line GEOINT/SIGINT pipeline skeleton dedupe (do it in S4, when a third agent shows its real shape);
- spec items 17 (`make demo-replay` / `smoke-live`) and 18 (`nexus-federation` network), which are Nice in the spec.

## Global Constraints

- One SQLite transaction + one timeline row per arrow, version-guarded. A writer that loses a race drops its write with `transition.dropped` (spec §9, ADR-008 F4). No status write bypasses `transition_task` / `transition_investigation`.
- No new tables or columns. Add the index `tasks(status, deadline)` only (spec §8.3).
- Task deadline is 180 s (`task_deadline_seconds`). Investigation deadline is 600 s. Poll is 2 s ± 20 %.
- Re-dispatch:
  - at most once, so `attempt` ∈ {1, 2};
  - fresh `deadline = min(now + 180 s, investigation deadline)`;
  - `token_reservation = floor(share × cap) − attempt-1 usage`, where unknown usage = 0.
- Readiness probe: `GET {agent}/readyz`, 2 s timeout (`health_probe_timeout_seconds`).
- Wait window (G7): `tasks.updated_at` → `min(updated_at + task_deadline_seconds, investigation deadline)`.
- Cancel (FR-125):
  - CS close: 1 s total timeout.
  - Agent cancels: 1 s total timeout, concurrent with each other, after the close, never in parallel with it.
  - Response within 2.5 s (NFR-102).
- Close reconciler runs at startup and every 30 s, leader-only.
- `NEXUS_MIN_ANALYSIS_TOKENS` defaults to 8000. In S3 the budget check is computed and logged only (G4).
- `NEXUS_REPLAY_LATENCY_MS` defaults to 0 and is refused when `llm_mode=live`. It is set only in `infrastructure/docker/compose.chaos.yaml`.
- New span names: `workflow.timeout`, `workflow.redispatch`, `workflow.cancel`, `workflow.reconcile_close`, `workflow.deadline`.
- G3: when specialists fail differently, `status_reason` is the first non-completed task's code in template order.
- G6: cancelling a terminal investigation → 409 `investigation_terminal`. Unknown → 404.
- G12: agent `FAILED(deadline_exceeded)` at attempt 1 → TIMED_OUT. On resume, poll before applying deadlines.
- NFR-101: a timeout is visible in the timeline within one poll interval (≤ 3 s).
- NFR-105: the in-process chaos suite runs in < 60 s, with no real sleep longer than 100 ms.
- Chaos runs in containers use `--no-deps` / `restart`, because the orchestrator `depends_on` geoint healthy.
- Commits: Conventional Commits, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never print or commit `.env` / `OPENAI_API_KEY`.
- Test command: `uv run --all-packages pytest <path> -q`. Lint: `make lint`. Schemas: `make schemas-check`.

## Review Focus

1. **Committed replay recordings vs the new guard (Task 1).** A recorded statement that names an `ev-*` id outside its candidate's evidence would now trigger a repair call that was never recorded. That gives REPLAY_MISS and breaks `make e2e-replay`. Expected: every committed recording still passes. A test in Task 1 replays all of them through the guard.
2. **A cancel racing a poll that is completing the same task (Tasks 8/9).** Expected: exactly one terminal write wins. The other is a `transition.dropped` note, never an exception or a 500. Tests in Tasks 8 and 9 interleave the two.
3. **The attempt-1 result arrives after attempt 2 was dispatched (Task 7).** A poll of attempt 1 must not overwrite the attempt-2 row. Expected: the late result is ignored (the agent GET returns the latest attempt; the orchestrator checks `attempt`). The test is in Task 7.
4. **Attempt-2 reservation ≤ 0 (Task 7).** Attempt 1 used more than its share, so `floor(share×cap) − usage ≤ 0`. Expected: no re-dispatch; the task stays terminal with its attempt-1 reason, and a log line says why. Ruling: dispatching a zero budget only buys a guaranteed `budget_exceeded`. The test is in Task 7.
5. **The orchestrator restarts with the investigation deadline already past (Task 11).** Expected: one poll per open task first (G12). Results that came in are kept, then FR-123 ends it as PARTIAL or FAILED(`investigation_deadline`). It never re-dispatches. The test is in Task 11.

---

### Task 1: Output-side statement/reasoning guard

**Files:**
- Modify: `shared/src/nexus_shared/agent/llm_steps.py` (`check_classifications`, new `text_violations`)
- Modify: `nodes/geoint-node/src/nexus_geoint/schemas.py` (`check_output`), `nodes/geoint-node/src/nexus_geoint/pipeline.py:298`
- Modify: `nodes/sigint-node/src/nexus_sigint/schemas.py` (`check_output`), `nodes/sigint-node/src/nexus_sigint/pipeline.py:299`
- Test: `shared/tests/agent/test_output_guard.py` (new), `contracts/tests/test_recordings_pass_guard.py` (new)

**Interfaces:**
- Produces:
  - `text_violations(field: str, text: str, allowed_refs: frozenset[str]) -> list[str]`. Messages are content-free and never echo the offending text, because the repair prompt would re-inject it.
  - `check_classifications(items, candidate_ids, statement_max, reasoning_max, *, allowed_refs: Mapping[str, frozenset[str]]) -> list[str]`. `allowed_refs` is keyed by candidate alias and is required, so neither agent can forget it.
  - `check_output(output, candidate_ids, allowed_refs)` in both agents.

Rules, applied to `statement` and `reasoning_summary`:

| Rule | Pattern | Message |
|---|---|---|
| fence | `<<<`, `>>>`, `UNTRUSTED_EVIDENCE`, `UNTRUSTED-TEXT` | `{alias}.{field} contains fence markers` |
| url | `(?i)\bhttps?://` or `(?i)\bwww\.` | `{alias}.{field} contains a URL` |
| token | `NEXUS-` (case-sensitive) | `{alias}.{field} contains a reserved token` |
| ref | `\bLOC-GRID-\d{2}\b`, `\bENT-[A-Z0-9-]+\b`, `\bHDL-[A-Z0-9-]+\b`, `\bev-[a-z]+-\d{3}\b`, not in `allowed_refs[alias]` | `{alias}.{field} references an id outside its candidate` |

`allowed_refs[alias]` = the candidate's `about` ids ∪ its `evidence_ids`. GEOINT `about` already includes the `LOC-GRID-nn` location. `HDL-` is my addition: SIGINT `about` holds handles, and the spec's list (LOC-GRID, ENT, ev) did not cover them.

- [ ] **Step 1: Write the failing tests** in `shared/tests/agent/test_output_guard.py`, using a stub `Classified` dataclass:
  - `test_clean_text_passes`: `text_violations("statement", "ALPHA and BRAVO co-located at LOC-GRID-07.", frozenset({"LOC-GRID-07"})) == []`.
  - `test_fence_marker_rejected`: `"<<<UNTRUSTED_EVIDENCE"` gives `["C1.statement contains fence markers"]` when called through `check_classifications`.
  - `test_url_rejected` (`"see https://x.io"` and `"www.x.io"`) and `test_reserved_token_rejected` (`"NEXUS-TRIPWIRE-G2L"`).
  - `test_foreign_location_rejected`: `"relocate to LOC-GRID-00"` with allowed `{"LOC-GRID-07"}` → ref message.
  - `test_foreign_evidence_rejected`: `ev-geo-005` not in the candidate's evidence → ref message.
  - `test_message_is_content_free`: no message contains the offending substring.
  - `test_repair_then_schema_invalid`: drive `classify_with_repair` with a fake provider that returns an injected statement twice. The result is `error == ErrorCode.SCHEMA_INVALID`, `usage.llm_calls == 2`, and the second call's `input_data["repair"]["errors"]` holds the ref message.
  - `test_repair_recovers`: the first output is injected, the second is clean, so `error is None`.

- [ ] **Step 2: Run them and confirm they fail.**
  Run: `uv run --all-packages pytest shared/tests/agent/test_output_guard.py -q`
  Expected: FAIL (`text_violations` not defined; `allowed_refs` unexpected keyword).

- [ ] **Step 3: Implement `text_violations` and extend `check_classifications`** in `llm_steps.py`. Run the text rules only on a non-empty field that is within its length limit, after the existing checks, one message per rule per field. Compile the patterns once at module level.

- [ ] **Step 4: Wire both agents.** In each pipeline, build `allowed_refs = {alias: frozenset(about_of(cand)) | frozenset(cand.evidence_ids)}` (GEOINT: `cand.about`; SIGINT: `[cand.subject, cand.object]`) from the same alias→candidate map passed to the LLM input. Pass it into `check_output`.

- [ ] **Step 5: Write `contracts/tests/test_recordings_pass_guard.py`.** For every `data/replay/*/*/*.json`:
  - parse `response`;
  - rebuild the candidates for that scenario through the agent's detector, the same way `scripts/record_replay.py` does;
  - assert that `check_output(...) == []` for each recording whose key material has `call_ordinal == 0` and that was accepted.

  If any recording fails, **stop and report BLOCKED**. Do not loosen the rules. A re-record needs a live key and the user's OK.

- [ ] **Step 6: Run the agent suites, the new test and the replay E2E unit tests.**
  Run: `uv run --all-packages pytest shared nodes contracts -q`
  Expected: all pass.

- [ ] **Step 7: Extend the injection test** in `nodes/geoint-node/tests/test_pipeline_injection.py` (the S3a T7 minor). Beyond the facts, also assert that `out.summary` and every `out.warnings` entry contain no tripwire from the scenario manifest.

- [ ] **Step 8: Commit**
  ```bash
  git commit -m "feat(agent): runtime output guard on statement and reasoning text"
  ```

### Task 2: Runtime hardening before re-dispatch

**Files:**
- Modify: `shared/src/nexus_shared/agent/runner.py` (CANCELED-write error path; merge `_cancel_current`/`_fail_latest`; `Phase = Literal["pending","run","commit"]`)
- Create: `shared/src/nexus_shared/agent/bands.py` (`BAND_MODERATE = 0.40`, `BAND_HIGH = 0.75`, `band_for(confidence: float) -> ConfidenceBand`)
- Modify: both `pipeline.py` files (import `band_for` from shared; delete the local copies and their `__all__` entries)
- Test: `shared/tests/agent/test_api_cancel.py`, `shared/tests/agent/test_runner.py`, `shared/tests/agent/test_bands.py`, `shared/tests/agent/test_lazy_app.py`

**Interfaces:**
- Produces: `nexus_shared.agent.bands.band_for`.
- Pins this agent behaviour, which Task 7 relies on:
  - `POST /v1/tasks` with attempt 2 supersedes a running attempt 1.
  - `GET /v1/tasks/{id}` returns the latest attempt.

- [ ] **Step 1: Write the failing and characterisation tests:**
  - `test_cancel_submitted_task_endpoint`: submit, then immediately `POST /v1/tasks/{id}/cancel` before the runner starts. Expect 200, status `CANCELED`, and a later GET still returns `CANCELED`.
  - `test_rejected_attempt2_supersedes_running_attempt1`: a slow attempt 1 is running; submit an attempt 2 that `validate_request` rejects. Expect attempt 1 interrupted, GET → attempt 2 `REJECTED`, and no batch committed. This is the S3a T4 ruling.
  - `test_canceled_write_failure_is_handled` (runner): make `store.mark_canceled` raise `aiosqlite.OperationalError`. Expect the runner logs, marks the row `FAILED(internal_error)` through the crash handler, and does not leak the exception out of the asyncio task. The test asserts that the task's exception is `None`.
  - `test_band_for_edges`: 0.39→LOW, 0.40→MODERATE, 0.749→MODERATE, 0.75→HIGH.
  - Replace the vacuous lazy-import test. It runs `python -c "import nexus_geoint.main"` in a `subprocess` with `env={"PATH": os.environ["PATH"]}` (no NEXUS_* vars) and asserts exit code 0. Do the same for `nexus_sigint.main`.

- [ ] **Step 2: Run them.** The endpoint tests may already pass, which is fine: they are guards. The CANCELED-write test and the band import must FAIL.
  Run: `uv run --all-packages pytest shared/tests/agent -q`

- [ ] **Step 3: Implement.**
  - Route the CANCELED write through the same `try/except` that wraps `spec.run`, so it has one crash handler.
  - Merge `_cancel_current` and `_fail_latest` into `_finish_latest(request, status, code | None)`.
  - Type `phase` as `Phase`.
  - Move `band_for`.

- [ ] **Step 4: Run `uv run --all-packages pytest shared nodes -q`.** Expected: all pass.

- [ ] **Step 5: Commit**
  ```bash
  git commit -m "fix(agent): handle cancel-write failures; share band_for; pin supersede behaviour"
  ```

### Task 3: Orchestrator foundations

**Files:**
- Create: `apps/orchestrator/src/nexus_orchestrator/clock.py`
- Modify: `settings.py`, `state/db.py`, `state/repository.py`, `main.py`, `workflow/poller.py`, `workflow/dispatch.py`, `workflow/creation.py` (replace `utcnow()` / `asyncio.sleep` with the clock)
- Modify: `apps/orchestrator/tests/orch_testkit.py` (add `FakeClock`; `make_app(clock=...)`)
- Test: `apps/orchestrator/tests/test_foundations.py` (new)

**Interfaces:**
- Produces:
  - `class Clock(Protocol)` with `def now(self) -> datetime` (UTC) and `async def sleep(self, seconds: float) -> None`.
  - `SystemClock`.
  - `FakeClock(start: datetime)`:
    - `now()` returns virtual time.
    - `sleep(s)` advances virtual time by `s` and then `await asyncio.sleep(0)`.
    - `advance(seconds: float) -> None`.
  - `create_app(..., clock: Clock | None = None)`. The clock is passed to the Poller, Dispatcher, InvestigationWorkflow and every new component below.
  - Settings:
    - `min_analysis_tokens: int = 8000` (env `NEXUS_MIN_ANALYSIS_TOKENS`, `ge=0`);
    - `reconcile_interval_seconds: float = 30.0`;
    - `cancel_step_timeout_seconds: float = 1.0`.
  - Index `ix_tasks_status_deadline ON tasks(status, deadline)`, created with `IF NOT EXISTS`.
  - `TaskRecord` gains:
    - `updated_at: datetime`;
    - `usage_tokens: int | None`, which is `input_tokens + output_tokens` from `usage_json`, or `None`.
  - `transition_task`:
    - When `fields` contains `"deadline"`, the emitted `TaskStatusChanged` carries that deadline.
    - When `to is PENDING`, it requires `fields["attempt"] == expected.attempt + 1 <= 2`, else it raises `IllegalTransition`. FR-122 is enforced at the write, not only in policy.

- [ ] **Step 1: Write the failing tests:**
  - `test_fake_clock_sleep_advances`.
  - `test_settings_min_analysis_tokens_default_and_env` (8000; env `NEXUS_MIN_ANALYSIS_TOKENS=5000` gives 5000).
  - `test_tasks_status_deadline_index_exists` (via `PRAGMA index_list(tasks)`).
  - `test_task_record_usage_tokens` (`usage_json` `{"input_tokens":3,"output_tokens":4,...}` → 7; NULL → None).
  - `test_redispatch_event_carries_deadline`.
  - `test_pending_requires_attempt_increment`: TIMED_OUT attempt 1 → PENDING with attempt 1 raises; attempt 2 applies; TIMED_OUT attempt 2 → PENDING with attempt 3 raises.

- [ ] **Step 2: Run them and confirm they fail.**
  Run: `uv run --all-packages pytest apps/orchestrator/tests/test_foundations.py -q`

- [ ] **Step 3: Implement.** Keep `ids.utcnow()` for code outside the workflow; inside workflow, state and API handlers, use `clock.now()`.

- [ ] **Step 4: Run the full orchestrator suite.** No behaviour change is expected.
  Run: `uv run --all-packages pytest apps/orchestrator -q`

- [ ] **Step 5: Commit** `feat(orchestrator): injectable clock, S3b settings, tasks(status,deadline) index`

### Task 4: Delegation clients: readiness, agent cancel, CS close

**Files:**
- Modify: `apps/orchestrator/src/nexus_orchestrator/delegation/agent_client.py`, `delegation/context_client.py`
- Test: `apps/orchestrator/tests/test_clients_s3b.py` (new; `httpx.MockTransport`)

**Interfaces:**
- Produces:
  - `AgentClient.ready(agent: AgentId) -> bool`:
    - `GET {url}/readyz`, `probe_timeout`.
    - True only on 200.
    - Never raises: connect/timeout/5xx → False.
    - An unknown agent → False.
  - `AgentClient.cancel(agent: AgentId, task_id: str) -> bool`:
    - `POST {url}/v1/tasks/{task_id}/cancel` with the orchestrator bearer token.
    - True on 200 or 404. 404 means the agent no longer knows the task, which is the goal anyway.
    - Never raises.
    - The caller bounds the total time; the client uses `probe_timeout` per call.
  - `ContextServiceClient.close(investigation_id: str, status: InvestigationStatus, *, timeout: float) -> bool`:
    - `POST /v1/investigations/{id}/close`, body `ContextInvestigationClose(status=...)`.
    - True on 200, or on 409 `investigation_cancelled`, which means it is already closed.
    - 404 → True. The CS never registered it, so nothing is left to fence.
    - Any other outcome → False.
    - Never raises.

- [ ] **Step 1: Write the failing tests:**
  - `test_ready_true_on_200`, `test_ready_false_on_503_timeout_connect_error`.
  - `test_cancel_sends_token_and_true_on_200_404`, `test_cancel_false_on_connect_error`.
  - `test_close_body_and_outcomes` (parametrized over 200→True, 404→True, 409 investigation_cancelled→True, 500→False, timeout→False).

- [ ] **Step 2: Run them; they FAIL** (no such method).

- [ ] **Step 3: Implement.** Each new call opens its own client span, following the existing `card()` pattern.

- [ ] **Step 4: Run them; they PASS.** Then run `uv run --all-packages pytest apps/orchestrator -q`.

- [ ] **Step 5: Commit** `feat(orchestrator): agent readiness/cancel and Context Service close clients`

### Task 5: Terminal policy (FR-124, FR-121a)

**Files:**
- Modify: `apps/orchestrator/src/nexus_orchestrator/workflow/terminal.py`, `workflow/poller.py` (`_maybe_finish` builds outcomes in template order)
- Test: `apps/orchestrator/tests/test_rules.py` (extend)

**Interfaces:**
- Produces:
  - `TaskOutcome(agent_id: AgentId, state: OrchestratorTaskState, reason: str | None = None, retry_open: bool = False)`.
  - `decide(tasks: Sequence[TaskOutcome], skipped: Sequence[AgentId]) -> TerminalDecision | None`. `tasks` must be in manifest template order.
- `decide` returns None when any task is non-terminal **or** has `retry_open`. Otherwise:

| Specialists COMPLETED | Result |
|---|---|
| no tasks at all | FAILED(agent_not_enabled) (unchanged) |
| all | PARTIAL(agent_not_enabled) |
| some | PARTIAL(reason of first non-COMPLETED task) |
| none | FAILED(reason of first non-COMPLETED task) |

  A non-completed task without a reason falls back to: TIMED_OUT → `deadline_exceeded`, otherwise `analysis_policy_unmet`. There is no `cancelled` ErrorCode, and a user-cancelled investigation never reaches `decide`. `retry_open` is computed by Task 7's `retry_window_open`. Until Task 7 lands, the poller passes False.

- [ ] **Step 1: Write the failing tests:**
  - `test_all_completed_partial_agent_not_enabled`.
  - `test_mixed_uses_first_failing_reason_in_template_order`: (SIGINT FAILED(task_lost), GEOINT TIMED_OUT(deadline_exceeded)) → none completed → FAILED(task_lost); swapped order → FAILED(deadline_exceeded).
  - `test_one_completed_one_timed_out_partial_deadline_exceeded`.
  - `test_retry_open_blocks_decision`.
  - `test_no_tasks_failed_agent_not_enabled`.

  Update the existing S2 tests that assert `analysis_policy_unmet` for a single failed specialist, since FR-124 changes that outcome. List each changed assertion in the report.

- [ ] **Step 2: Run them; they FAIL.**

- [ ] **Step 3: Implement the change.** `_maybe_finish` orders the task records by `manifest.template` index and passes `status_reason` through.

- [ ] **Step 4: Run `uv run --all-packages pytest apps/orchestrator -q`.** It passes.

- [ ] **Step 5: Commit** `feat(orchestrator): specialist terminal policy uses the failing task's reason`

### Task 6: Deadline watcher (FR-120, FR-121b)

**Files:**
- Create: `apps/orchestrator/src/nexus_orchestrator/workflow/deadlines.py`
- Modify: `workflow/poller.py` (tick order; `_record` maps FR-121b)
- Test: `apps/orchestrator/tests/test_deadlines.py` (new; `FakeClock`, `FakeAgent`)

**Interfaces:**
- Consumes: `Clock`, `AgentClient.cancel`, `transition_task`.
- Produces:
  - `expired(tasks: Sequence[TaskRecord], now: datetime) -> list[TaskRecord]`: PENDING/DISPATCHED/RUNNING with `now >= deadline`.
  - `Poller._time_out(task: TaskRecord) -> None`:
    - span `workflow.timeout`;
    - → TIMED_OUT, reason `deadline_exceeded`;
    - after commit, a best-effort `agents.cancel(agent, task_id)` for DISPATCHED/RUNNING only.
- The tick order inside `_drive` becomes:
  1. investigation deadline (Task 8);
  2. task deadlines;
  3. re-dispatch (Task 7);
  4. dispatch PENDING;
  5. poll DISPATCHED/RUNNING;
  6. finish.
- FR-121b: in `_record`, an agent `FAILED` with code `deadline_exceeded` at `attempt == 1` is written as TIMED_OUT (reason `deadline_exceeded`), not FAILED.

- [ ] **Step 1: Write the failing tests:**
  - `test_running_task_times_out_at_deadline`:
    - `FakeAgent` acks and then stays RUNNING;
    - advance the clock 180 s and run one tick;
    - the task is TIMED_OUT(deadline_exceeded);
    - a `task.status_changed` event exists;
    - the fake agent saw one cancel POST;
    - one `workflow.timeout` span exists.
  - `test_pending_unreachable_task_times_out`: the agent is down, the task stays PENDING across ticks, and after 180 s it is TIMED_OUT with no cancel POST.
  - `test_agent_deadline_exceeded_recorded_as_timed_out_attempt1`.
  - `test_agent_deadline_exceeded_attempt2_is_failed`: at attempt 2, the poll FAILED(deadline_exceeded) stays FAILED.
  - `test_timeout_visible_within_one_tick` (NFR-101): the deadline passes mid-sleep, and the event appears on the next tick.
  - `test_timeout_loses_race_to_completion`: the poll writes COMPLETED and then the watcher runs with the stale version. The result is a `transition.dropped` note and COMPLETED kept.

- [ ] **Step 2: Run them; they FAIL.**

- [ ] **Step 3: Implement.** The watcher reads tasks inside the tick, not from a cached list, so it sees the current versions.

- [ ] **Step 4: Run them; they PASS.** Then run the full orchestrator suite.

- [ ] **Step 5: Commit** `feat(orchestrator): task deadline watcher (FR-120, FR-121b)`

### Task 7: Single re-dispatch (FR-121, FR-121a, FR-122)

**Files:**
- Modify: `workflow/deadlines.py`, `workflow/poller.py`, `workflow/dispatch.py` (dispatch is callable for any PENDING task on any tick)
- Test: `apps/orchestrator/tests/test_redispatch.py` (new)

**Interfaces:**
- Consumes: `AgentClient.ready`, `TaskRecord.updated_at/usage_tokens`, the Task 3 attempt guard.
- Produces:
  - `retry_eligible(task: TaskRecord) -> bool`: `attempt == 1` and (TIMED_OUT, or FAILED with reason ∈ `REDISPATCHABLE_CODES`).
  - `retry_window_end(task: TaskRecord, investigation_deadline: datetime, settings) -> datetime`.
  - `retry_window_open(task, investigation_deadline, now, settings) -> bool` (eligible and `now < window_end`). Task 5 `TaskOutcome.retry_open` uses it.
  - `attempt2_reservation(share: float, cap: int, usage_tokens: int | None) -> int`, which is `floor(share*cap) - (usage_tokens or 0)`.
  - `Poller._redispatch(task, investigation, manifest) -> None`:
    - span `workflow.redispatch`;
    - only when `retry_window_open`, `await agents.ready(agent)` is True and the reservation is > 0;
    - writes → PENDING with fields `attempt=2`, `deadline=min(now+task_deadline, inv.deadline)` and `token_reservation=reservation`.
  - The same tick's dispatch step then POSTs it. `build_request` already reads attempt, deadline and reservation from the row.
- Stale-attempt guard: `_poll_one` ignores a `TaskResult` whose `attempt` ≠ `task.attempt`. It does not write and logs at debug.

- [ ] **Step 1: Write the failing tests:**
  - `test_timed_out_redispatched_when_ready`:
    - the reservation is 15000 and attempt-1 usage unknown;
    - after the timeout, with the agent ready, one tick moves the task to PENDING attempt 2 with deadline `now+180` and reservation 15000;
    - the next POST body has `attempt == 2`, `token_budget == 15000` and the same `deadline`.
  - `test_reservation_reduced_by_attempt1_usage`: FAILED(agent_restarted) with `usage_json` totalling 4000 gives 11000.
  - `test_not_ready_waits_then_final`:
    - `/readyz` is 503;
    - ticks across 179 s leave the task TIMED_OUT and the investigation RUNNING (FR-121a);
    - at 180 s the window closes, and the investigation ends PARTIAL or FAILED(`deadline_exceeded`) per Task 5.
  - `test_ready_mid_window_redispatches`: not ready for 20 s, then ready, so attempt 2 is dispatched.
  - `test_window_capped_by_investigation_deadline`: the investigation deadline is 60 s away, so the window ends at 60 s.
  - `test_attempt2_failure_terminal` (FR-122): attempt 2 TIMED_OUT stays TIMED_OUT, with no third POST.
  - `test_internal_error_not_redispatched`.
  - `test_zero_reservation_not_redispatched` (Review Focus 4): usage 15000 means no re-dispatch, a log record, and a final status.
  - `test_late_attempt1_result_ignored` (Review Focus 3): after attempt 2 is dispatched, `FakeAgent` returns an attempt-1 COMPLETED. The task row stays at attempt 2 DISPATCHED.
  - `test_task_lost_redispatch`: the agent `forget()`s, so the poll returns 404 → FAILED(task_lost) → ready → attempt 2.

- [ ] **Step 2: Run them; they FAIL.**

- [ ] **Step 3: Implement.** Do the readiness check outside any DB transaction. Take the transition's `expected` from a re-read after the readiness await, so a concurrent cancel wins.

- [ ] **Step 4: Run `uv run --all-packages pytest apps/orchestrator -q`.** It passes.

- [ ] **Step 5: Commit** `feat(orchestrator): single re-dispatch with readiness wait window (FR-121/122)`

### Task 8: Fence routine and investigation deadline (FR-123)

**Files:**
- Create: `apps/orchestrator/src/nexus_orchestrator/workflow/fence.py`
- Modify: `workflow/poller.py` (deadline check first in tick), `main.py` (construct `Fence`, pass it to Poller and Runtime), `api/runtime.py`
- Test: `apps/orchestrator/tests/test_fence.py` (new)

**Interfaces:**
- Consumes: `ContextServiceClient.close`, `AgentClient.cancel`, `Clock`, `settings.cancel_step_timeout_seconds`.
- Produces:
  - `class Fence(settings, db, repo, context, agents, tracer, clock)`.
  - `async def end(self, investigation_id: str, status: InvestigationStatus, reason: str | None, *, task_reason: str | None) -> FenceResult`.
  - `FenceResult(applied: bool, status: InvestigationStatus, context_closed: bool)`.
- `end` follows this order, and every step after (1) runs even when an earlier network step fails:
  1. In one transaction: `transition_investigation(→status, reason)` and set `context_closed=0`. If the result is not APPLIED, return `applied=False` with the current status.
  2. `asyncio.wait_for(context.close(id, status, timeout=t), t)`. On True, set `context_closed=1`.
  3. Agent cancels for the non-terminal tasks, via `asyncio.wait_for(asyncio.gather(..., return_exceptions=True), t)`.
  4. Each non-terminal task → CANCELLED (reason `task_reason`). A DROPPED result is fine.
- FR-123, in `Poller`: at the start of each tick, if `now >= inv.deadline`:
  - span `workflow.deadline`;
  - `status = PARTIAL if any specialist COMPLETED else FAILED`;
  - `await fence.end(id, status, "investigation_deadline", task_reason="investigation_deadline")`;
  - the driver exits.

- [ ] **Step 1: Write the failing tests:**
  - `test_deadline_with_one_completed_partial` (AC13-d):
    - SIGINT COMPLETED and GEOINT RUNNING;
    - advance past 600 s;
    - the result is investigation PARTIAL(investigation_deadline), GEOINT CANCELLED, a CS close with status PARTIAL, a GEOINT cancel POST, `context_closed == 1`, and a `workflow.deadline` span.
  - `test_deadline_with_none_completed_failed`.
  - `test_fence_order_close_before_agent_cancel`: record the call order on a shared list; `close` comes before every `cancel`.
  - `test_fence_cs_down_context_closed_0`: close times out, the agent cancels still happen, and the tasks are still CANCELLED.
  - `test_fence_agent_hangs_bounded`: the agent cancel never returns, so `end` returns within `2 * cancel_step_timeout + 0.1` s wall time. Use a real 1 s timeout scaled down via settings to 0.05.
  - `test_completion_racing_fence` (Review Focus 2): the task completes between steps 1 and 4. Its CANCELLED write is DROPPED, with no exception.
  - `test_no_investigation_stays_running_past_deadline` (NFR-100): neither agent ever answers. At 600 s + one tick, the status is terminal.

- [ ] **Step 2: Run them; they FAIL.**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them; they PASS.** Then run the full orchestrator suite.

- [ ] **Step 5: Commit** `feat(orchestrator): fence-first terminal routine and investigation deadline (FR-123)`

### Task 9: Cancel endpoint (FR-125)

**Files:**
- Modify: `apps/orchestrator/src/nexus_orchestrator/api/routes.py`, `workflow/poller.py` (stop the driver on cancel)
- Test: `apps/orchestrator/tests/test_cancel.py` (new)

**Interfaces:**
- Consumes: `Fence.end`, `InvestigationCancelResponse{id, status, context_closed}` (exists from S3a), `ErrorCode.INVESTIGATION_TERMINAL`.
- Produces: `POST /api/v1/investigations/{id}/cancel`:
  - 200 `InvestigationCancelResponse`;
  - 404 when unknown;
  - 409 problem with `code=investigation_terminal` and the current status in `detail` when the investigation is already terminal, or when `Fence.end` returns `applied=False`.
  - Span `workflow.cancel` around `Fence.end(id, CANCELLED, None, task_reason=None)`. No new ErrorCode: the CANCELLED state is the reason.
  - Cancel works from CREATED and RUNNING.
  - After a cancel the route does not stop the driver: the driver sees the investigation is no longer RUNNING and exits on its next tick (as shipped; `stop_one` was dropped because cancelling a driver mid-submit orphaned the agent task). The creation retrier drops the id.

- [ ] **Step 1: Write the failing tests:**
  - `test_cancel_running_returns_cancelled_context_closed_true`: the tasks are CANCELLED, the fake agents saw cancels, and the response is `{"status":"CANCELLED","context_closed":true}`.
  - `test_cancel_cs_down_context_closed_false`.
  - `test_cancel_within_budget_when_everything_hangs` (NFR-102): CS and agents hang, the timeout setting is 1.0, and the response arrives in < 2.5 s of wall time. Mark it `@pytest.mark.slow` and keep it in CI; it is the only real-time test.
  - `test_cancel_terminal_409_investigation_terminal`.
  - `test_cancel_unknown_404`.
  - `test_cancel_created_investigation` (CS registration pending).
  - `test_cancel_vs_completion_race`: the poll completes the last task concurrently with the cancel. The investigation ends either CANCELLED or PARTIAL, never 500, and the timeline holds exactly one investigation terminal event.

- [ ] **Step 2: Run them; they FAIL.**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them; they PASS.** Then run `make schemas-check` and the full orchestrator suite.

- [ ] **Step 5: Commit** `feat(orchestrator): investigation cancel endpoint (FR-125)`

### Task 10: Close reconciler (FR-126)

**Files:**
- Create: `apps/orchestrator/src/nexus_orchestrator/workflow/reconciler.py`
- Modify: `state/repository.py` (`list_unclosed_terminal() -> list[tuple[str, InvestigationStatus]]`, `mark_context_closed(tx, id) -> None`), `main.py` (start after the sweep, stop in `_shutdown`), `api/runtime.py`
- Test: `apps/orchestrator/tests/test_reconciler.py` (new)

**Interfaces:**
- Produces:
  - `CloseReconciler(settings, repo, db, context, tracer, clock, is_leader)` with `async run_once() -> int` (count closed), `start()` and `stop()`.
  - The loop runs `run_once` at start, then every `reconcile_interval_seconds` via `clock.sleep`.
  - It skips the pass when `is_leader()` is False.
  - Each investigation it tries gets one span, `workflow.reconcile_close`.
  - Close uses `context_service_timeout_seconds`.
  - It never raises out of the loop.
- Also covers the normal finish path: PARTIAL and FAILED investigations from `_maybe_finish` now reach the CS close through the reconciler, within 30 s.

- [ ] **Step 1: Write the failing tests:**
  - `test_run_once_closes_unclosed_terminal` (context_closed becomes 1; the body status is the investigation status).
  - `test_run_once_skips_non_terminal_and_closed`.
  - `test_cs_down_then_up_closes_next_pass` (AC13-c in-process): the first pass leaves it 0; `FakeClock.advance(30)` makes the next pass 1.
  - `test_not_leader_skips`.
  - `test_runs_at_startup` (app lifespan with `FakeContextService` gives a close request before the first advance).

- [ ] **Step 2: Run them; they FAIL.**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them; they PASS.**

- [ ] **Step 5: Commit** `feat(orchestrator): leader-only Context Service close reconciler (FR-126)`

### Task 11: Resume sweep and budget check (FR-127, FR-128)

**Files:**
- Modify: `workflow/creation.py` (`resume_sweep`), `workflow/poller.py` (`ensure(id, *, resumed: bool = False)`; on the first resumed tick, poll before deadlines), `workflow/dispatch.py` (re-POST PENDING with the stored attempt and deadline; already the case, assert it)
- Create: `workflow/budget.py`
- Test: `apps/orchestrator/tests/test_resume.py` (new), `apps/orchestrator/tests/test_budget.py` (new)

**Interfaces:**
- Produces:
  - `Poller.ensure(investigation_id, *, resumed=False)`. When `resumed`, the first tick runs: dispatch PENDING → poll DISPATCHED/RUNNING once → then deadlines (task and investigation) → re-dispatch → finish. Later ticks use the normal order.
  - `resume_sweep` calls `poller.ensure(id, resumed=True)` for RUNNING investigations.
  - `analysis_budget_short(cap: int, used: int, min_analysis: int) -> bool`, which is `cap - used < min_analysis`.
  - After each COMPLETED task the Poller evaluates it. Analysis is not enabled in S3 (`ANALYSIS ∈ NOT_YET_DISPATCHABLE`), so the result is only logged: `log.info("analysis budget short", extra={...remaining, min})`, with no status change.

- [ ] **Step 1: Write the failing tests:**
  - `test_resume_repost_pending_same_attempt_and_deadline`: seed a RUNNING investigation with a PENDING attempt-2 task. Start the app; the POST body has attempt 2 and the stored deadline byte-for-byte.
  - `test_resume_polls_before_deadline_completed_kept`: the deadline passed during the outage and the agent reports COMPLETED, so the result is COMPLETED, not TIMED_OUT.
  - `test_resume_polls_before_deadline_agent_timeout_is_timed_out` (FR-121b on resume).
  - `test_resume_no_answer_then_timed_out`.
  - `test_resume_after_investigation_deadline` (Review Focus 5): it polls first, then ends PARTIAL or FAILED(investigation_deadline) with no re-dispatch POST.
  - `test_budget_short_logged_not_enforced`: cap 60000, used 55000, so there is a log record and the status is unchanged.
  - `test_budget_threshold_edges` (`analysis_budget_short(60000, 52000, 8000) is False`, `52001 → True`).

- [ ] **Step 2: Run them; they FAIL.**

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run them; they PASS.** Then run the full orchestrator suite.

- [ ] **Step 5: Commit** `feat(orchestrator): poll-first resume sweep and analysis budget check (FR-127/128)`

### Task 12: In-process chaos suite (AC12-b, AC13-d, §12)

**Files:**
- Create: `apps/orchestrator/tests/chaos/__init__.py`, `apps/orchestrator/tests/chaos/conftest.py`, `apps/orchestrator/tests/chaos/test_failure_matrix.py`
- Modify: `apps/orchestrator/tests/orch_testkit.py` (FakeAgent knobs: `hang()`, `restart()` → next poll FAILED(agent_restarted), `down()`/`up()` for submit and readyz, `answer_after(seconds)`)

**Interfaces:**
- Consumes: everything from Tasks 3–11; `FakeClock`.
- Produces: a `chaos_app` fixture that wires two `FakeAgent`s (SIGINT, GEOINT), `FakeContextService` and `FakeClock`, with `drive(seconds)`. `drive` advances the clock in poll-interval steps and yields to the loop until the driver is idle.

- [ ] **Step 1: Write one test per §12 row with an in-process owner**, each asserting the final investigation status/reason and the task attempt/status rows:

| Test | Setup | Expected |
|---|---|---|
| `test_ac12b_never_answers_twice` | GEOINT hangs on both attempts | TIMED_OUT → attempt 2 with a fresh deadline and reduced budget → TIMED_OUT; PARTIAL(deadline_exceeded); SIGINT findings kept |
| `test_agent_down_before_ack` | GEOINT down | PENDING → TIMED_OUT → window closes → PARTIAL(deadline_exceeded) |
| `test_agent_restarted_mid_task` | GEOINT `restart()` | attempt 2 COMPLETED; PARTIAL(agent_not_enabled) |
| `test_task_lost` | GEOINT `forget()` | FAILED(task_lost) → attempt 2 COMPLETED |
| `test_ac13d_investigation_deadline` | SIGINT completes, GEOINT hangs past 600 s with its retry also hanging | GEOINT CANCELLED; PARTIAL(investigation_deadline) |
| `test_cancel_cs_down_then_reconciled` | CS down, cancel, CS up, drive 30 s | CANCELLED immediately with `context_closed` 0 → 1 |
| `test_orchestrator_restart_mid_run` | stop the app after dispatch, recreate it on the same DB file | same terminal status as the uninterrupted run; one `finding_ids` set |
| `test_internal_error_not_redispatched` | GEOINT FAILED(internal_error) | FAILED → PARTIAL(internal_error); one POST |

- [ ] **Step 2: Run the suite and time it.**
  Run: `uv run --all-packages pytest apps/orchestrator/tests/chaos -q --durations=5`
  Expected: all pass, total < 60 s (NFR-105), and no single test over 10 s. Any failure here is a defect in Tasks 5–11: fix it in the owning module, commit it as `fix(orchestrator): …`, and list it in the report.

- [ ] **Step 3: Commit** `test(orchestrator): in-process chaos suite for the S3 failure matrix`

### Task 13: `make chaos` (AC12-a, AC13-a/b/c)

**Files:**
- Modify: `shared/src/nexus_shared/llm/replay.py` (`ReplayLLMProvider(replay_root, *, latency_ms: int = 0)`, sleeping after a hit), the agent settings (`replay_latency_ms: int = 0`, env `NEXUS_REPLAY_LATENCY_MS`, `ge=0`; a validator rejects a non-zero value when `llm_mode == "live"`), and the provider factory
- Create: `infrastructure/docker/compose.chaos.yaml` (override: `NEXUS_TASK_DEADLINE_SECONDS=45` on the orchestrator; `NEXUS_REPLAY_LATENCY_MS=20000` and `NEXUS_LLM_MODE=replay` on both agents)
- Create: `scripts/chaos.sh`
- Modify: `Makefile` (a `chaos` target and `.PHONY`)
- Test: `shared/tests/llm/test_replay_latency.py`

**Interfaces:**
- `scripts/chaos.sh [scenario...]`, run against `compose.yaml` + `compose.chaos.yaml`. The default runs all four:
  - `ac12a`: stop geoint, create, assert GEOINT TIMED_OUT within 45 + 3 s, no attempt 2 while it is down, then PARTIAL(deadline_exceeded) with SIGINT findings.
  - `ac13a`: create, `restart geoint-agent` mid-task, assert attempt 2 COMPLETED and the AC3-a terminal status.
  - `ac13b`: create, `kill orchestrator` after dispatch, `start --no-deps orchestrator`, assert the same terminal status and no duplicate findings.
  - `ac13c`: stop context-service, cancel, assert CANCELLED with `context_closed:false`, start context-service, assert `context_closed:true` within 30 s.
- The script uses `--no-deps` / `restart` throughout, prints one PASS/FAIL line per scenario, exits non-zero on any FAIL, and never prints `.env`.

`45` s is my call: AC12-a then finishes in about 2 × 45 s instead of about 360 s (G7 cost, §18). `20000` ms is longer than the restart/kill window and shorter than 45 s.

- [ ] **Step 1: Write the failing tests:**
  - `test_latency_zero_default_no_sleep`.
  - `test_latency_applied_on_hit` (monkeypatch `asyncio.sleep`; called with 0.5 for 500).
  - `test_latency_refused_in_live_mode` (settings validation error).
  - `test_replay_output_identical_with_latency`.

- [ ] **Step 2: Run them; they FAIL.** Implement the change, run them, and they PASS.
  Run: `uv run --all-packages pytest shared/tests/llm -q`

- [ ] **Step 3: Write the compose override, `scripts/chaos.sh` and the Makefile target.** Then run `make lint` and the AC5 lint test: `uv run --all-packages pytest tests/infra -q`.

- [ ] **Step 4: Run it.**
  Run: `make chaos`
  Expected: 4 PASS lines. Paste the output into the report. If macOS Docker timing flakes (§18), record which scenario and its timings, then **report DONE_WITH_CONCERNS**. Do not extend the timeouts silently.

- [ ] **Step 5: Commit** `feat(chaos): replay latency knob and make chaos for AC12-a, AC13-a/b/c`

### Task 14: UI cancel and task rows (FR-150)

**Files:**
- Modify: `apps/frontend/src/api/generated/*` (regenerate from `contracts/schemas` with the existing frontend types script in `apps/frontend/package.json`; this adds `InvestigationCancelResponse` and `TaskCancelResponse`)
- Modify: `apps/frontend/src/api/client.ts` (`cancelInvestigation(id: string): Promise<InvestigationCancelResponse>`)
- Modify: `apps/frontend/src/pages/investigation-detail.tsx`
- Test: `apps/frontend/src/pages/investigation-detail.test.tsx` (new or extend), `apps/frontend/e2e/s3-cancel.spec.ts` (new)

**Interfaces:**
- Cancel button:
  - visible only while the status is non-terminal (CREATED/RUNNING/ANALYZING);
  - opens a confirm dialog (`Cancel investigation?` / `Cancel investigation` / `Keep running`) built from the existing `components/ui` primitives;
  - on confirm, calls `cancelInvestigation`, then shows the returned status and a muted `Context closed` or `Context close pending` line;
  - a 409 is shown via `problem-alert`.
- Task rows: one row per task, with agent, `attempt {n}`, status badge, and `status_reason` in mono when present.

- [ ] **Step 1: Write the failing unit tests:**
  - `hides cancel for terminal status`.
  - `confirm calls cancelInvestigation and shows CANCELLED`.
  - `409 shows problem alert`.
  - `renders two task rows with attempt and reason`.

- [ ] **Step 2: Run the frontend unit tests; they FAIL.** Implement, then run types, lint, unit and build; all PASS.

- [ ] **Step 3: Write the e2e spec** `s3-cancel.spec.ts`: create an investigation on the replay stack, click Cancel, confirm, and expect the status badge to read `CANCELLED`. Run it against `make up` with the chaos override, so the latency keeps it RUNNING long enough.

- [ ] **Step 4: Commit** `feat(frontend): cancel investigation and per-task attempt/reason rows (FR-150)`

### Task 15: E2E hardening and S3b verification

**Files:**
- Modify: `scripts/e2e_replay.py`, `Makefile` (`e2e-replay` log gate)
- Test: `contracts/tests/test_agent_cancel_contract.py` (new)

- [ ] **Step 1: Fix the S3a T11 minors in `scripts/e2e_replay.py`:**
  - The poll loop checks the HTTP status and fails fast on non-2xx.
  - A missing key raises a per-scenario FAIL, and the remaining scenarios still run.
  - The timeline is read with cursor paging, not `limit=100`.
  - Every task's `TaskResult.summary` and `warnings` are screened against the scenario's tripwires.
  - The Makefile gate enforces `done_n >= 6` (2 agents × 3 scenarios).

- [ ] **Step 2: Write `test_agent_cancel_contract.py`.** Run it against the in-process shared agent app with a slow pipeline: submit, cancel while RUNNING, and expect 200 `TaskCancelResponse` with status CANCELED. The agent's later commit attempt is never made.

- [ ] **Step 3: Full verification.** Each command and its expected result:
  - `make lint`: clean.
  - `make schemas-check`: no drift.
  - `uv run --all-packages pytest -q`: all pass (S3a baseline 646 + the new tests).
  - `make e2e-replay`: 3/3 and zero OpenAI calls.
  - `make chaos`: 4/4 PASS.
  - Frontend: types, lint, unit, build and e2e all pass.

  Paste every output into the report.

- [ ] **Step 4: Commit** `test(e2e): harden replay E2E and add agent cancel contract test`

### Task 16: Context Service cross-caller hardening (Good-to-have)

**Files:**
- Modify: `context-service/src/nexus_context_service/repository.py`, `routes.py`
- Test: `context-service/tests/test_findings_batch_caller.py` (extend)

- [ ] **Step 1: Write the failing tests:**
  - `test_cross_caller_writes_denied_audit_row`: one `AuditEvent` with outcome `DENIED`, action `findings.submit` and the caller, and no finding ids.
  - `test_cross_caller_to_other_investigation_403`.
  - `test_cross_caller_to_closed_investigation`: 409 `investigation_cancelled` takes precedence over 403 and does not reveal ownership.
  - `test_403_detail_is_generic`: the detail equals `"forbidden"` and does not contain the task_id.

- [ ] **Step 2: Run them; they FAIL.** Implement, then run `uv run --all-packages pytest context-service -q`; it PASSES.

- [ ] **Step 3: Commit** `fix(context-service): audit and harden cross-caller batch denial`

### Task 17: Oracle-free agent manifests (Good-to-have)

**Files:**
- Modify: `scripts/gen_scenario.py` (write `data/agent-manifests/<scenario>.json` = the manifest minus `oracle`; `--check` covers it)
- Create: `data/agent-manifests/*.json` (generated, committed)
- Modify: `infrastructure/docker/compose.yaml` (agents mount `data/agent-manifests` ro instead of `data/scenarios`), the agent settings default path, and `tests/infra/test_compose_isolation.py` (allowlist)
- Modify: spec §10 wording (drop "an oracle-free agent projection … is an S3b item")
- Test: `contracts/tests/test_agent_manifests.py` (new)

- [ ] **Step 1: Write the failing tests:**
  - `test_agent_manifest_has_no_oracle` (for every file, `"oracle" not in json`).
  - `test_agent_manifest_equals_manifest_minus_oracle`.
  - The AC5 allowlist test asserts that the agents no longer mount `data/scenarios`.

- [ ] **Step 2: Run them; they FAIL.** Implement, regenerate, and run `uv run --all-packages pytest contracts tests/infra nodes -q` and `make e2e-replay`. All pass; the replay keys are unchanged, because agents never hashed the oracle.

- [ ] **Step 3: Commit** `feat(agents): mount oracle-free scenario manifests`
