"""Analysis budget check (S3b FR-128): after each COMPLETED task, is ``cap - used`` below
``NEXUS_MIN_ANALYSIS_TOKENS``? Analysis is not enabled in S3, so it is only logged."""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from orch_testkit import FakeAgent, FakeContextService, S, create, running, runtime, wait_until

from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.state.repository import TaskRecord
from nexus_orchestrator.workflow.budget import analysis_budget_short, tokens_used

POLLER = "nexus_orchestrator.workflow.poller"


def test_budget_threshold_edges() -> None:
    assert analysis_budget_short(60000, 52000, 8000) is False  # exactly the minimum is enough
    assert analysis_budget_short(60000, 52001, 8000) is True
    assert analysis_budget_short(60000, 0, 8000) is False
    assert analysis_budget_short(60000, 65000, 8000) is True  # overspent


def _task(usage_tokens: int | None) -> TaskRecord:
    now = utcnow()
    return TaskRecord(
        id="tsk_x",
        investigation_id="inv_x",
        agent_id=FakeAgent().agent_id,
        task_type="t",
        status=T.COMPLETED,
        status_reason=None,
        attempt=1,
        deadline=datetime.max.replace(tzinfo=now.tzinfo),
        token_reservation=1,
        acked_at=None,
        version=1,
        updated_at=now,
        usage_tokens=usage_tokens,
    )


def test_tokens_used_counts_unknown_as_zero() -> None:
    assert tokens_used([_task(100), _task(None), _task(250)]) == 350
    assert tokens_used([]) == 0


def short_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "analysis budget short"]


async def completed(app, inv_id: str) -> bool:
    [task] = await runtime(app).repo.task_records(inv_id)
    return task.status is T.COMPLETED


async def test_budget_short_logged_not_enforced(make_app, caplog) -> None:
    """Cap 60000, used 55000: 5000 left is under the 8000 minimum. A log record, no status
    change: the investigation ends the way it would have without the check."""
    agent = FakeAgent(script=[S.COMPLETED], usage=(50000, 5000))
    app = make_app(FakeContextService(), agent)
    with caplog.at_level(logging.INFO, logger=POLLER):
        async with running(app) as client:
            inv_id = await create(client)
            await wait_until(lambda: completed(app, inv_id))
            inv = await runtime(app).repo.get_investigation(inv_id)

    [record] = short_records(caplog)
    assert record.investigation_id == inv_id  # type: ignore[attr-defined]
    assert record.remaining == 5000 and record.min == 8000  # type: ignore[attr-defined]
    assert record.cap == 60000 and record.used == 55000  # type: ignore[attr-defined]
    assert inv is not None and inv.status_reason != "budget_exceeded"


async def test_budget_not_short_is_silent(make_app, caplog) -> None:
    agent = FakeAgent(script=[S.COMPLETED], usage=(120, 30))
    app = make_app(FakeContextService(), agent)
    with caplog.at_level(logging.INFO, logger=POLLER):
        async with running(app) as client:
            inv_id = await create(client)
            await wait_until(lambda: completed(app, inv_id))

    assert short_records(caplog) == []


@pytest.mark.parametrize("script", [[S.FAILED]])
async def test_budget_only_checked_after_completed(make_app, caplog, script) -> None:
    agent = FakeAgent(script=script, usage=(59000, 0))
    app = make_app(FakeContextService(), agent)
    with caplog.at_level(logging.INFO, logger=POLLER):
        async with running(app) as client:
            inv_id = await create(client)

            async def failed() -> bool:
                [task] = await runtime(app).repo.task_records(inv_id)
                return task.status is T.FAILED

            await wait_until(failed)

    assert short_records(caplog) == []
