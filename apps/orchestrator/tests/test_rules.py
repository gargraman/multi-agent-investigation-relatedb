"""Pure rules: transition tables (§8.2), the S2 terminal rule (FR-007/G1), S2 settings.

The expected tables are written out again here on purpose, as an independent copy: a change to
``workflow/transitions.py`` has to be made twice to pass.
"""

from __future__ import annotations

import itertools

import pytest
from pydantic import ValidationError

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus as I
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_contracts.common import AgentId
from nexus_orchestrator.workflow import terminal
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


def o(agent: AgentId, state: T) -> terminal.TaskOutcome:
    return terminal.TaskOutcome(agent, state)


@pytest.mark.parametrize(
    "tasks,skipped,expected",
    [
        # still running: no decision
        ([o(SIG, T.RUNNING)], [GEO, ANA], None),
        ([o(SIG, T.PENDING)], [GEO, ANA], None),
        ([o(SIG, T.COMPLETED), o(GEO, T.DISPATCHED)], [ANA], None),
        # row 1: no task rows at all
        ([], [SIG, GEO, ANA], (I.FAILED, ErrorCode.AGENT_NOT_ENABLED)),
        # row 2: no specialist COMPLETED
        ([o(SIG, T.FAILED)], [GEO, ANA], (I.FAILED, ErrorCode.ANALYSIS_POLICY_UNMET)),
        ([o(SIG, T.CANCELLED)], [GEO, ANA], (I.FAILED, ErrorCode.ANALYSIS_POLICY_UNMET)),
        ([o(SIG, T.TIMED_OUT)], [GEO, ANA], (I.FAILED, ErrorCode.ANALYSIS_POLICY_UNMET)),
        # row 3: the S2 demo path
        ([o(SIG, T.COMPLETED)], [GEO, ANA], (I.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)),
        ([o(SIG, T.FAILED), o(GEO, T.COMPLETED)], [ANA], (I.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)),
        # row 4: nothing skipped
        ([o(SIG, T.COMPLETED)], [], (I.PARTIAL, ErrorCode.ANALYSIS_POLICY_UNMET)),
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
