"""Pure rules: transition tables (§8.2), the S2 terminal rule (FR-007/G1), S2 settings.

The expected tables are written out again here on purpose, as an independent copy: a change to
``workflow/transitions.py`` has to be made twice to pass.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from orch_testkit import SCENARIO, SCENARIOS_DIR
from pydantic import ValidationError

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus as I
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_contracts.common import AgentId
from nexus_contracts.scenario.loader import load_manifest
from nexus_orchestrator.state.repository import TaskRecord
from nexus_orchestrator.workflow import terminal
from nexus_orchestrator.workflow.poller import outcomes_in_template_order
from nexus_orchestrator.workflow.transitions import (
    IllegalTransition,
    check_investigation,
    check_task,
    check_task_insert,
    investigation_allowed,
    task_allowed,
)

INV_ALLOWED = {
    (I.CREATED, I.RUNNING),
    (I.CREATED, I.FAILED),
    (I.CREATED, I.CANCELLED),
    (I.RUNNING, I.ANALYZING),
    (I.RUNNING, I.PARTIAL),
    (I.RUNNING, I.FAILED),
    (I.RUNNING, I.CANCELLED),
    (I.ANALYZING, I.COMPLETED),
    (I.ANALYZING, I.PARTIAL),
    (I.ANALYZING, I.FAILED),
    (I.ANALYZING, I.CANCELLED),
}

TASK_ALLOWED = {
    (T.PENDING, T.DISPATCHED),
    (T.PENDING, T.FAILED),
    (T.PENDING, T.TIMED_OUT),
    (T.PENDING, T.CANCELLED),
    (T.DISPATCHED, T.RUNNING),
    (T.DISPATCHED, T.COMPLETED),
    (T.DISPATCHED, T.FAILED),
    (T.DISPATCHED, T.TIMED_OUT),
    (T.DISPATCHED, T.CANCELLED),
    (T.RUNNING, T.COMPLETED),
    (T.RUNNING, T.FAILED),
    (T.RUNNING, T.TIMED_OUT),
    (T.RUNNING, T.CANCELLED),
    (T.TIMED_OUT, T.PENDING),  # S3 re-dispatch
    # FAILED → PENDING only for a re-dispatchable reason: tested separately below.
}


@pytest.mark.parametrize("src,dst", list(itertools.product(I, I)))
def test_investigation_edges(src: I, dst: I) -> None:
    allowed = (src, dst) in INV_ALLOWED
    assert investigation_allowed(src, dst) is allowed
    if allowed:
        check_investigation(src, dst)
    else:
        with pytest.raises(IllegalTransition):
            check_investigation(src, dst)


@pytest.mark.parametrize("src,dst", list(itertools.product(T, T)))
def test_task_edges_without_reason(src: T, dst: T) -> None:
    allowed = (src, dst) in TASK_ALLOWED
    assert task_allowed(src, dst) is allowed
    if allowed:
        check_task(src, dst)
    else:
        with pytest.raises(IllegalTransition):
            check_task(src, dst)


@pytest.mark.parametrize("reason", list(ErrorCode))
def test_failed_to_pending_only_for_redispatchable_reasons(reason: ErrorCode) -> None:
    expected = reason in {ErrorCode.AGENT_RESTARTED, ErrorCode.TASK_LOST}
    assert task_allowed(T.FAILED, T.PENDING, src_reason=reason) is expected
    # Every other edge out of FAILED stays forbidden whatever the reason (sticky terminal).
    for dst in T:
        if dst is not T.PENDING:
            assert not task_allowed(T.FAILED, dst, src_reason=reason)


@pytest.mark.parametrize("state", list(T))
def test_task_insert_only_as_pending(state: T) -> None:
    if state is T.PENDING:
        check_task_insert(state)
    else:
        with pytest.raises(IllegalTransition):
            check_task_insert(state)


def test_terminal_states_are_sticky() -> None:
    for src in (I.COMPLETED, I.PARTIAL, I.FAILED, I.CANCELLED):
        assert not any(investigation_allowed(src, d) for d in I)
    for src in (T.COMPLETED, T.CANCELLED):
        assert not any(task_allowed(src, d) for d in T)


# -- terminal rule ----------------------------------------------------------------------------

SIG, GEO, ANA = AgentId.SIGINT, AgentId.GEOINT, AgentId.ANALYSIS


def o(
    agent: AgentId, state: T, reason: str | None = None, retry_open: bool = False
) -> terminal.TaskOutcome:
    return terminal.TaskOutcome(agent, state, reason, retry_open)


@pytest.mark.parametrize(
    "tasks,skipped,expected",
    [
        # still running: no decision
        ([o(SIG, T.RUNNING)], [GEO, ANA], None),
        ([o(SIG, T.PENDING)], [GEO, ANA], None),
        ([o(SIG, T.COMPLETED), o(GEO, T.DISPATCHED)], [ANA], None),
        # FR-124: no task rows at all
        ([], [SIG, GEO, ANA], (I.FAILED, ErrorCode.AGENT_NOT_ENABLED)),
        # FR-124: no specialist COMPLETED, reason of the failing task (fallback when absent)
        ([o(SIG, T.FAILED)], [GEO, ANA], (I.FAILED, ErrorCode.ANALYSIS_POLICY_UNMET)),
        ([o(SIG, T.CANCELLED)], [GEO, ANA], (I.FAILED, ErrorCode.ANALYSIS_POLICY_UNMET)),
        # FR-124 fallback: a TIMED_OUT task without a reason is deadline_exceeded
        ([o(SIG, T.TIMED_OUT)], [GEO, ANA], (I.FAILED, ErrorCode.DEADLINE_EXCEEDED)),
        # FR-124: all specialists COMPLETED (the S2 demo path)
        ([o(SIG, T.COMPLETED)], [GEO, ANA], (I.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)),
        # FR-124: some completed, reason is the first non-COMPLETED task's (here: the fallback)
        (
            [o(SIG, T.FAILED), o(GEO, T.COMPLETED)],
            [ANA],
            (I.PARTIAL, ErrorCode.ANALYSIS_POLICY_UNMET),
        ),
        # FR-124: all specialists completed is PARTIAL(agent_not_enabled) whether or not skipped
        ([o(SIG, T.COMPLETED)], [], (I.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)),
        # an analysis task COMPLETED does not count as a specialist
        ([o(ANA, T.COMPLETED)], [SIG], (I.FAILED, ErrorCode.ANALYSIS_POLICY_UNMET)),
    ],
)
def test_terminal_decide(tasks, skipped, expected) -> None:
    decision = terminal.decide(tasks, skipped)
    if expected is None:
        assert decision is None
    else:
        assert decision == terminal.TerminalDecision(*expected)
        # every outcome is a legal edge out of RUNNING
        assert investigation_allowed(I.RUNNING, decision.status)


def test_all_completed_partial_agent_not_enabled() -> None:
    decision = terminal.decide([o(SIG, T.COMPLETED), o(GEO, T.COMPLETED)], [ANA])
    assert decision == terminal.TerminalDecision(I.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)


def test_mixed_uses_first_failing_reason_in_template_order() -> None:
    sig = o(SIG, T.FAILED, ErrorCode.TASK_LOST.value)
    geo = o(GEO, T.TIMED_OUT, ErrorCode.DEADLINE_EXCEEDED.value)
    assert terminal.decide([sig, geo], []) == terminal.TerminalDecision(
        I.FAILED, ErrorCode.TASK_LOST
    )
    assert terminal.decide([geo, sig], []) == terminal.TerminalDecision(
        I.FAILED, ErrorCode.DEADLINE_EXCEEDED
    )


def test_one_completed_one_timed_out_partial_deadline_exceeded() -> None:
    tasks = [o(SIG, T.COMPLETED), o(GEO, T.TIMED_OUT, ErrorCode.DEADLINE_EXCEEDED.value)]
    assert terminal.decide(tasks, [ANA]) == terminal.TerminalDecision(
        I.PARTIAL, ErrorCode.DEADLINE_EXCEEDED
    )


def test_retry_open_blocks_decision() -> None:
    tasks = [o(SIG, T.FAILED, ErrorCode.LLM_UNAVAILABLE.value, retry_open=True)]
    assert terminal.decide(tasks, [GEO, ANA]) is None
    tasks = [o(SIG, T.COMPLETED), o(GEO, T.FAILED, ErrorCode.LLM_UNAVAILABLE.value, True)]
    assert terminal.decide(tasks, [ANA]) is None


def test_no_tasks_failed_agent_not_enabled() -> None:
    assert terminal.decide([], [SIG, GEO, ANA]) == terminal.TerminalDecision(
        I.FAILED, ErrorCode.AGENT_NOT_ENABLED
    )


def test_unknown_reason_string_falls_back() -> None:
    decision = terminal.decide([o(SIG, T.FAILED, "not-a-code")], [])
    assert decision == terminal.TerminalDecision(I.FAILED, ErrorCode.ANALYSIS_POLICY_UNMET)


RECORD_UPDATED = datetime(2026, 1, 1, tzinfo=UTC)
NOW = RECORD_UPDATED + timedelta(days=30)  # long after any retry window of ``_record`` rows
FAR = RECORD_UPDATED + timedelta(days=365)  # investigation deadline


def _record(agent: AgentId, state: T, reason: str | None = None) -> TaskRecord:
    return TaskRecord(
        id=f"tsk_{agent.value}",
        investigation_id="inv_1",
        agent_id=agent,
        task_type="t",
        status=state,
        status_reason=reason,
        attempt=1,
        deadline=datetime(2026, 1, 1, tzinfo=UTC),
        token_reservation=1000,
        acked_at=None,
        version=1,
        updated_at=RECORD_UPDATED,
    )


def test_outcomes_follow_manifest_template_order(make_settings) -> None:
    full = load_manifest(SCENARIOS_DIR / SCENARIO)
    sig_geo = [t for t in full.template.tasks if t.agent in (SIG, GEO)]
    assert [t.agent for t in sig_geo] == [SIG, GEO]  # the premise of this test
    manifest = full.model_copy(
        update={"template": full.template.model_copy(update={"tasks": sig_geo})}
    )
    # as ``ORDER BY created_at, id`` can return them: reversed, and an off-template agent first
    records = [
        _record(ANA, T.COMPLETED),
        _record(GEO, T.FAILED, ErrorCode.TASK_LOST.value),
        _record(SIG, T.TIMED_OUT, ErrorCode.DEADLINE_EXCEEDED.value),
    ]
    outcomes = outcomes_in_template_order(manifest, records, FAR, NOW, make_settings())
    assert [x.agent_id for x in outcomes] == [SIG, GEO, ANA]
    assert [x.reason for x in outcomes] == [ErrorCode.DEADLINE_EXCEEDED.value, "task_lost", None]
    assert [x.state for x in outcomes] == [T.TIMED_OUT, T.FAILED, T.COMPLETED]
    assert not any(x.retry_open for x in outcomes)
    # the order decides the investigation reason: SIGINT's failure comes first
    assert terminal.decide(outcomes, []) == terminal.TerminalDecision(
        I.FAILED, ErrorCode.DEADLINE_EXCEEDED
    )


def test_outcomes_without_a_manifest_keep_input_order(make_settings) -> None:
    records = [_record(GEO, T.COMPLETED), _record(SIG, T.FAILED, "task_lost")]
    outcomes = outcomes_in_template_order(None, records, FAR, NOW, make_settings())
    assert [x.agent_id for x in outcomes] == [GEO, SIG]
    assert outcomes[1].reason == "task_lost" and not outcomes[1].retry_open


def test_outcomes_mark_a_task_with_an_open_retry_window_retry_open(make_settings) -> None:
    settings = make_settings()
    records = [
        _record(SIG, T.TIMED_OUT, ErrorCode.DEADLINE_EXCEEDED.value),
        _record(GEO, T.COMPLETED),
    ]
    soon = RECORD_UPDATED + timedelta(seconds=10)  # inside the 180 s window
    open_ = outcomes_in_template_order(None, records, FAR, soon, settings)
    assert [x.retry_open for x in open_] == [True, False]
    assert terminal.decide(open_, []) is None  # FR-121a: the investigation is not finished
    # investigation deadline closes it sooner than the 180 s
    capped = outcomes_in_template_order(
        None, records, RECORD_UPDATED + timedelta(seconds=5), soon, settings
    )
    assert not any(x.retry_open for x in capped)
    assert terminal.decide(capped, []) is not None


# -- settings ---------------------------------------------------------------------------------


def test_settings_defaults_and_env_parsing(make_settings, monkeypatch) -> None:
    s = make_settings()
    assert s.nexus_enabled_agents == (AgentId.SIGINT,)
    assert s.agent_url(AgentId.SIGINT) == "http://sigint-agent:8000"
    assert s.task_deadline_seconds == 180
    monkeypatch.setenv("NEXUS_ENABLED_AGENTS", "sigint-agent, geoint-agent")
    monkeypatch.setenv(
        "NEXUS_AGENT_URLS",
        '{"sigint-agent": "http://sig:8000/", "geoint-agent": "https://geo:9000"}',
    )
    s = make_settings()
    assert s.nexus_enabled_agents == (AgentId.SIGINT, AgentId.GEOINT)
    assert s.agent_url(AgentId.SIGINT) == "http://sig:8000"  # trailing slash stripped
    monkeypatch.setenv("NEXUS_ENABLED_AGENTS", "")
    assert make_settings().nexus_enabled_agents == ()


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"nexus_enabled_agents": "sigint-agent,sigint-agent"}, "duplicates"),
        ({"nexus_enabled_agents": "analysis-agent"}, "S4"),
        ({"nexus_enabled_agents": "geoint-agent"}, "no URL"),
        ({"nexus_agent_urls": {"sigint-agent": "ftp://x"}}, "http(s)"),
        ({"nexus_enabled_agents": "nope-agent"}, "nope-agent"),
    ],
)
def test_settings_reject_inconsistent_agent_registry(make_settings, overrides, message) -> None:
    with pytest.raises(ValidationError) as err:
        make_settings(**overrides)
    assert message in str(err.value)
