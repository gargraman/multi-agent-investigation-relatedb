"""S2 terminal rule (S2 spec FR-007 as revised by G1; main spec §8.2, §8.4).

Evaluated only once **every task row is terminal**. S2 has no ANALYZING phase, so a RUNNING
investigation can only end PARTIAL or FAILED (§8.2 has no RUNNING→COMPLETED edge).

=========================================================  ==================================
Situation (all task rows terminal)                         Investigation
=========================================================  ==================================
no task rows at all (no template agent enabled)            FAILED  (agent_not_enabled)
no specialist task COMPLETED                               FAILED  (analysis_policy_unmet)
≥1 specialist COMPLETED and ≥1 template agent skipped      PARTIAL (agent_not_enabled)
≥1 specialist COMPLETED, nothing skipped                   PARTIAL (analysis_policy_unmet)
=========================================================  ==================================

Notes:

- Row 3 is the S2 demo path: SIGINT COMPLETED, GEOINT and Analysis skipped.
- FR-007 says "SIGINT COMPLETED". This generalises it to "a specialist COMPLETED" (§8.4: analysis
  runs if at least one specialist completed). With only SIGINT enabled they are identical.
- Row 4 is unreachable with the shipped templates (they all contain ``analysis-agent``, which S2
  refuses to enable, so something is always skipped). If a template had no analysis task, §8.2's
  "RUNNING → PARTIAL (analysis skipped: policy not met)" is the only edge that fits.
- Row 1: FR-007's "otherwise FAILED(analysis_policy_unmet)" would hide the real cause, so the
  G1 code is used. ``skipped_agents`` on the RUNNING event lists every template agent.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class TerminalDecision:
    status: InvestigationStatus
    reason: ErrorCode


def decide(tasks: Sequence[TaskOutcome], skipped: Sequence[AgentId]) -> TerminalDecision | None:
    """``None`` while any task is still non-terminal."""
    if any(not t.state.is_terminal for t in tasks):
        return None
    if not tasks:
        return TerminalDecision(InvestigationStatus.FAILED, ErrorCode.AGENT_NOT_ENABLED)
    completed = any(
        t.agent_id in SPECIALISTS and t.state is OrchestratorTaskState.COMPLETED for t in tasks
    )
    if not completed:
        return TerminalDecision(InvestigationStatus.FAILED, ErrorCode.ANALYSIS_POLICY_UNMET)
    if skipped:
        return TerminalDecision(InvestigationStatus.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)
    return TerminalDecision(InvestigationStatus.PARTIAL, ErrorCode.ANALYSIS_POLICY_UNMET)
