"""Investigation terminal policy for specialists (FR-124; S2 FR-007 as revised by G1; §8.2, §8.4).

Evaluated only once **every task row is terminal** and no failed task still has a retry window
open (``TaskOutcome.retry_open``). ``tasks`` must be in manifest template order. A RUNNING
investigation can only end PARTIAL or FAILED (§8.2 has no RUNNING→COMPLETED edge).

=========================================================  ==================================
Situation (all task rows terminal, none retry-open)        Investigation
=========================================================  ==================================
no task rows at all (no template agent enabled)            FAILED  (agent_not_enabled)
every specialist task COMPLETED (≥1)                       PARTIAL (agent_not_enabled)
some specialist COMPLETED, some task not                   PARTIAL (reason of first non-COMPLETED)
no specialist COMPLETED                                    FAILED  (reason of first non-COMPLETED)
=========================================================  ==================================

Notes:

- "Reason of the first non-COMPLETED task" is that task's ``status_reason``, in template order.
  Without one: TIMED_OUT gives ``deadline_exceeded``, anything else ``analysis_policy_unmet``. There
  is no ``cancelled`` ErrorCode, and a user-cancelled investigation never reaches ``decide``.
- The all-specialists-COMPLETED row is the S2 demo path: SIGINT COMPLETED, GEOINT and Analysis
  skipped. The analysis agent is not enabled yet, so the investigation stays PARTIAL.
- FR-007 says "SIGINT COMPLETED". This generalises it to "a specialist COMPLETED" (§8.4: analysis
  runs if at least one specialist completed).
- Row 1: FR-007's "otherwise FAILED(analysis_policy_unmet)" would hide the real cause, so the
  G1 code is used. ``skipped_agents`` on the RUNNING event lists every template agent.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus, OrchestratorTaskState
from nexus_contracts.common import AgentId

SPECIALISTS = frozenset({AgentId.SIGINT, AgentId.GEOINT})


@dataclass(frozen=True)
class TaskOutcome:
    agent_id: AgentId
    state: OrchestratorTaskState
    reason: str | None = None  # the task row's ``status_reason``
    retry_open: bool = False  # a failed task whose retry window is still open: not final yet


@dataclass(frozen=True)
class TerminalDecision:
    status: InvestigationStatus
    reason: ErrorCode


def _reason(task: TaskOutcome) -> ErrorCode:
    """The failing task's own code; a missing or unknown one falls back per FR-124."""
    if task.reason is not None:
        with contextlib.suppress(ValueError):
            return ErrorCode(task.reason)
    if task.state is OrchestratorTaskState.TIMED_OUT:
        return ErrorCode.DEADLINE_EXCEEDED
    return ErrorCode.ANALYSIS_POLICY_UNMET


def decide(tasks: Sequence[TaskOutcome], skipped: Sequence[AgentId]) -> TerminalDecision | None:
    """``None`` while any task is non-terminal or still has a retry window open.

    ``tasks`` must be in manifest template order. ``skipped`` no longer changes the outcome: it
    is kept for the callers that already pass it (the RUNNING event lists it).
    """
    if any(not t.state.is_terminal or t.retry_open for t in tasks):
        return None
    if not tasks:
        return TerminalDecision(InvestigationStatus.FAILED, ErrorCode.AGENT_NOT_ENABLED)
    specialists = [t for t in tasks if t.agent_id in SPECIALISTS]
    done = [t for t in specialists if t.state is OrchestratorTaskState.COMPLETED]
    if specialists and len(done) == len(specialists):
        return TerminalDecision(InvestigationStatus.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)
    failing = next((t for t in tasks if t.state is not OrchestratorTaskState.COMPLETED), None)
    reason = _reason(failing) if failing else ErrorCode.ANALYSIS_POLICY_UNMET
    return TerminalDecision(
        InvestigationStatus.PARTIAL if done else InvestigationStatus.FAILED, reason
    )
