"""Exhaustive test of the §8.2 agent status → orchestrator task state mapping."""

import pytest

from nexus_contracts.a2a import (
    AgentTaskStatus,
    PollFailure,
    TaskResult,
    TaskStateUpdate,
    map_poll_outcome,
    map_task_result,
)
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import OrchestratorTaskState as S

# One row per line of the §8.2 table.
TABLE = [
    (AgentTaskStatus.SUBMITTED, None, TaskStateUpdate(S.DISPATCHED)),
    (AgentTaskStatus.WORKING, None, TaskStateUpdate(S.RUNNING)),
    (AgentTaskStatus.COMPLETED, None, TaskStateUpdate(S.COMPLETED)),
    (
        AgentTaskStatus.FAILED,
        ErrorCode.AGENT_RESTARTED,
        TaskStateUpdate(S.FAILED, ErrorCode.AGENT_RESTARTED, True),
    ),
    (AgentTaskStatus.REJECTED, None, TaskStateUpdate(S.FAILED, ErrorCode.TASK_REJECTED, False)),
    (AgentTaskStatus.CANCELED, None, TaskStateUpdate(S.CANCELLED)),
    (PollFailure.NOT_FOUND, None, TaskStateUpdate(S.FAILED, ErrorCode.TASK_LOST, True)),
    (PollFailure.TRANSIENT, None, TaskStateUpdate(None)),
]


@pytest.mark.parametrize("outcome,code,expected", TABLE)
def test_table_row(outcome, code, expected) -> None:
    assert map_poll_outcome(outcome, code) == expected


def test_table_covers_every_status_and_poll_failure() -> None:
    covered = {row[0] for row in TABLE}
    assert covered == set(AgentTaskStatus) | set(PollFailure)


@pytest.mark.parametrize("code", [c for c in ErrorCode if c is not ErrorCode.AGENT_RESTARTED])
def test_failed_other_code_is_not_redispatched(code: ErrorCode) -> None:
    update = map_poll_outcome(AgentTaskStatus.FAILED, code)
    assert update.state is S.FAILED and update.reason is code
    # task_lost never comes from an agent, but if it did it would still be re-dispatchable.
    assert update.redispatchable is (code is ErrorCode.TASK_LOST)


def test_failed_without_code_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        map_poll_outcome(AgentTaskStatus.FAILED)


def test_canceled_spelling_crosses_only_here() -> None:
    assert AgentTaskStatus.CANCELED.value == "CANCELED"
    assert S.CANCELLED.value == "CANCELLED"
    assert "CANCELLED" not in {s.value for s in AgentTaskStatus}
    assert "CANCELED" not in {s.value for s in S}


def test_map_task_result_uses_error_code() -> None:
    result = TaskResult(
        task_id="tsk_1",
        agent_id="sigint-agent",
        status="FAILED",
        attempt=1,
        llm_mode="live",
        error={"category": "lifecycle", "code": "agent_restarted"},
    )
    assert map_task_result(result) == TaskStateUpdate(S.FAILED, ErrorCode.AGENT_RESTARTED, True)
