"""Analysis budget check (S3b FR-128): after each COMPLETED task, is ``cap - used`` below
``NEXUS_MIN_ANALYSIS_TOKENS``? Analysis is not enabled in S3, so it is only logged."""

from __future__ import annotations

import logging

from orch_testkit import (
    FakeAgent,
    FakeClock,
    FakeContextService,
    S,
    create,
    parked,
    pump,
    running,
    runtime,
    task_in,
    wait_until,
)

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus as I
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_orchestrator.ids import utcnow
from nexus_orchestrator.workflow.budget import analysis_budget_short

POLLER = "nexus_orchestrator.workflow.poller"
# One COMPLETED specialist, the other template agents not enabled: PARTIAL(agent_not_enabled).
EXPECTED_END = (I.PARTIAL, ErrorCode.AGENT_NOT_ENABLED)


def test_budget_threshold_edges() -> None:
    assert analysis_budget_short(60000, 52000, 8000) is False  # exactly the minimum is enough
    assert analysis_budget_short(60000, 52001, 8000) is True
    assert analysis_budget_short(60000, 0, 8000) is False
    assert analysis_budget_short(60000, 65000, 8000) is True  # overspent


def short_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "analysis budget short"]


async def finished(app, inv_id: str) -> bool:
    inv = await runtime(app).repo.get_investigation(inv_id)
    return inv is not None and inv.status.is_terminal


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
            await wait_until(lambda: finished(app, inv_id))
            inv = await runtime(app).repo.get_investigation(inv_id)

    [record] = short_records(caplog)
    assert record.investigation_id == inv_id  # type: ignore[attr-defined]
    assert record.remaining == 5000 and record.min == 8000  # type: ignore[attr-defined]
    assert record.cap == 60000 and record.used == 55000  # type: ignore[attr-defined]
    # the end is what the terminal rule gives for one COMPLETED specialist, budget or not
    assert inv is not None and (inv.status, inv.status_reason) == EXPECTED_END


async def test_budget_not_short_is_silent(make_app, caplog) -> None:
    agent = FakeAgent(script=[S.COMPLETED], usage=(120, 30))
    app = make_app(FakeContextService(), agent)
    with caplog.at_level(logging.INFO, logger=POLLER):
        async with running(app) as client:
            inv_id = await create(client)
            await wait_until(lambda: completed(app, inv_id))

    assert short_records(caplog) == []


async def test_budget_only_checked_after_completed(make_app, caplog) -> None:
    agent = FakeAgent(script=[S.FAILED], usage=(59000, 0))
    agent.ready = False  # no attempt 2 that could complete and log
    app = make_app(FakeContextService(), agent)
    with caplog.at_level(logging.INFO, logger=POLLER):
        async with running(app) as client:
            inv_id = await create(client)
            await wait_until(task_in(app, inv_id, T.FAILED))

    assert short_records(caplog) == []


async def test_budget_used_includes_a_failed_first_attempt(make_app, caplog) -> None:
    """Attempt 1 FAILED with 2000 tokens, attempt 2 COMPLETED with 2500: ``used`` is the sum
    (the investigation's cumulative ledger), not just the last attempt's usage."""
    clock = FakeClock(utcnow())
    agent = FakeAgent(
        script=[S.WORKING, S.FAILED], fail_code=ErrorCode.AGENT_RESTARTED, usage=(2000, 0)
    )
    app = make_app(
        FakeContextService(),
        agent,
        clock=clock,
        nexus_max_tokens_per_investigation=20000,  # SIGINT share 0.25: reservation 5000
        min_analysis_tokens=19000,  # 20000 - 4500 = 15500 left: short
    )
    with caplog.at_level(logging.INFO, logger=POLLER):
        async with running(app) as client:
            inv_id = await create(client)
            await pump(clock, task_in(app, inv_id, T.FAILED))
            agent.script, agent.usage = [S.COMPLETED], (2500, 0)  # attempt 2 will complete
            await parked(clock)
            await pump(clock, attempt2_completed(app, inv_id))

    [record] = short_records(caplog)
    assert record.used == 4500 and record.remaining == 15500  # type: ignore[attr-defined]


def attempt2_completed(app, inv_id: str):
    async def check() -> bool:
        [task] = await runtime(app).repo.task_records(inv_id)
        return task.attempt == 2 and task.status is T.COMPLETED

    return check
