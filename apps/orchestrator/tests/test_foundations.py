"""S3b foundations: injectable clock, new settings, deadline index, TaskRecord additions and the
FR-122 attempt guard on the re-dispatch edge."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from orch_testkit import FakeClock

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import OrchestratorTaskState as T
from nexus_contracts.common import AgentId
from nexus_orchestrator.ids import iso
from nexus_orchestrator.pubsub import TimelineBroker
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.repository import Applied, Repository, TaskRecord
from nexus_orchestrator.workflow.transitions import IllegalTransition

START = datetime(2026, 1, 1, tzinfo=UTC)
INV = "inv_01TESTFOUNDATIONS00000000"
TASK = "tsk_01TESTFOUNDATIONS00000000"
ENV = {"SERVICE_TOKEN": "t" * 32}


async def test_fake_clock_sleep_advances() -> None:
    clock = FakeClock(START)
    assert clock.now() == START
    sleeper = asyncio.create_task(clock.sleep(2.5))
    await asyncio.sleep(0)
    assert clock.pending() == 1 and clock.now() == START  # sleeping does not move time
    clock.advance(2.5)
    await sleeper
    assert clock.now() == START + timedelta(seconds=2.5) and clock.pending() == 0
    await clock.sleep(0)  # zero and negative sleeps just yield
    await clock.sleep(-3)
    assert clock.now() == START + timedelta(seconds=2.5)


async def test_fake_clock_sleep_blocks_until_advance() -> None:
    clock = FakeClock(START)
    sleeper = asyncio.create_task(clock.sleep(5))
    await asyncio.sleep(0)
    clock.advance(4)
    await asyncio.sleep(0)
    assert not sleeper.done()
    clock.advance(1)
    await asyncio.sleep(0)
    assert sleeper.done()


async def test_fake_clock_run_for_wakes_in_order() -> None:
    clock = FakeClock(START)
    woke: list[tuple[str, float]] = []

    async def sleeper(name: str, seconds: float, times: int = 1) -> None:
        for _ in range(times):
            await clock.sleep(seconds)
            woke.append((name, (clock.now() - START).total_seconds()))

    tasks = [
        asyncio.create_task(sleeper("slow", 7)),
        asyncio.create_task(sleeper("fast", 2, times=3)),  # re-sleeps after each wake
        asyncio.create_task(sleeper("mid", 4)),
    ]
    await asyncio.sleep(0)
    await clock.run_for(10)
    await asyncio.gather(*tasks)
    assert woke == [("fast", 2), ("mid", 4), ("fast", 4), ("fast", 6), ("slow", 7)]
    assert clock.now() == START + timedelta(seconds=10)


def test_settings_min_analysis_tokens_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NEXUS_MIN_ANALYSIS_TOKENS", raising=False)
    default = OrchestratorSettings()  # type: ignore[call-arg]
    assert default.min_analysis_tokens == 8000
    assert default.reconcile_interval_seconds == 30.0
    assert default.cancel_step_timeout_seconds == 1.0
    monkeypatch.setenv("NEXUS_MIN_ANALYSIS_TOKENS", "5000")
    assert OrchestratorSettings().min_analysis_tokens == 5000  # type: ignore[call-arg]
    monkeypatch.setenv("NEXUS_MIN_ANALYSIS_TOKENS", "-1")
    with pytest.raises(ValueError):
        OrchestratorSettings()  # type: ignore[call-arg]


@pytest.fixture
async def repo(tmp_path: Path):
    db = Database(tmp_path / "foundations.sqlite")
    await db.open()
    yield Repository(db, TimelineBroker())
    await db.close()


async def _seed_task(repo: Repository) -> TaskRecord:
    async with repo.db.transaction() as tx:
        await repo.insert_investigation(
            tx,
            investigation_id=INV,
            scenario_id="s",
            objective="o",
            token_budget=60000,
            deadline=START + timedelta(minutes=10),
            trace_context_json=None,
            now=START,
        )
        await repo.insert_task(
            tx,
            task_id=TASK,
            investigation_id=INV,
            agent_id=AgentId.SIGINT,
            task_type="analyze_synthetic_communications",
            deadline=START + timedelta(minutes=3),
            token_reservation=1000,
            now=START,
        )
    [task] = await repo.task_records(INV)
    return task


async def _current(repo: Repository) -> TaskRecord:
    [task] = await repo.task_records(INV)
    return task


async def test_tasks_status_deadline_index_exists(repo: Repository) -> None:
    rows = await repo.db.fetchall("PRAGMA index_list(tasks)")
    assert "ix_tasks_status_deadline" in {r["name"] for r in rows}
    info = await repo.db.fetchall("PRAGMA index_info(ix_tasks_status_deadline)")
    assert [r["name"] for r in sorted(info, key=lambda r: r["seqno"])] == ["status", "deadline"]


async def test_task_record_usage_tokens(repo: Repository) -> None:
    task = await _seed_task(repo)
    assert task.usage_tokens is None
    assert task.updated_at == START
    usage = {"input_tokens": 3, "output_tokens": 4, "llm_calls": 1}
    async with repo.db.transaction() as tx:
        await tx.execute("UPDATE tasks SET usage_json = ? WHERE id = ?", (json.dumps(usage), TASK))
    assert (await _current(repo)).usage_tokens == 7
    for bad in ("null", "[1, 2]", "7", '"x"', "{not json", '{"input_tokens": "a"}'):
        async with repo.db.transaction() as tx:
            await tx.execute("UPDATE tasks SET usage_json = ? WHERE id = ?", (bad, TASK))
        assert (await _current(repo)).usage_tokens is None, bad


async def test_redispatch_event_carries_deadline(repo: Repository) -> None:
    task = await _seed_task(repo)
    later = START + timedelta(minutes=5)
    new_deadline = later + timedelta(minutes=3)
    async with repo.db.transaction() as tx:
        await repo.transition_task(
            tx, task, T.TIMED_OUT, now=later, reason=ErrorCode.DEADLINE_EXCEEDED
        )
    timed_out = await _current(repo)
    async with repo.db.transaction() as tx:
        applied = await repo.transition_task(
            tx,
            timed_out,
            T.PENDING,
            now=later,
            fields={"attempt": 2, "deadline": iso(new_deadline)},
        )
    assert applied is Applied.APPLIED
    events = await repo.events_after(INV, 0, 100)
    payload = events[-1].payload
    assert payload["to_state"] == "PENDING" and payload["attempt"] == 2
    assert datetime.fromisoformat(payload["deadline"]) == new_deadline
    # A transition that does not touch the deadline leaves the field out of the event.
    assert events[-2].payload["deadline"] is None


async def test_deadline_field_accepts_datetime_or_iso(repo: Repository) -> None:
    task = await _seed_task(repo)
    new_deadline = START + timedelta(minutes=9)
    async with repo.db.transaction() as tx:
        await repo.transition_task(
            tx, task, T.TIMED_OUT, now=START, reason=ErrorCode.DEADLINE_EXCEEDED
        )
    async with repo.db.transaction() as tx:
        await repo.transition_task(
            tx,
            await _current(repo),
            T.PENDING,
            now=START,
            fields={"attempt": 2, "deadline": new_deadline},
        )
    assert (await _current(repo)).deadline == new_deadline
    payload = (await repo.events_after(INV, 0, 100))[-1].payload
    assert datetime.fromisoformat(payload["deadline"]) == new_deadline


async def test_pending_requires_attempt_increment(repo: Repository) -> None:
    task = await _seed_task(repo)
    now = START + timedelta(minutes=4)
    async with repo.db.transaction() as tx:
        await repo.transition_task(
            tx, task, T.TIMED_OUT, now=now, reason=ErrorCode.DEADLINE_EXCEEDED
        )

    # attempt 1 -> PENDING with the same attempt: refused
    with pytest.raises(IllegalTransition):
        async with repo.db.transaction() as tx:
            await repo.transition_task(
                tx, await _current(repo), T.PENDING, now=now, fields={"attempt": 1}
            )
    with pytest.raises(IllegalTransition):  # and with no attempt at all
        async with repo.db.transaction() as tx:
            await repo.transition_task(tx, await _current(repo), T.PENDING, now=now)
    assert (await _current(repo)).status is T.TIMED_OUT  # nothing was written

    # attempt 2 applies
    async with repo.db.transaction() as tx:
        applied = await repo.transition_task(
            tx, await _current(repo), T.PENDING, now=now, fields={"attempt": 2}
        )
    assert applied is Applied.APPLIED
    assert (await _current(repo)).attempt == 2

    # attempt 2 -> TIMED_OUT -> PENDING with attempt 3: refused (at most one re-dispatch)
    async with repo.db.transaction() as tx:
        await repo.transition_task(
            tx, await _current(repo), T.TIMED_OUT, now=now, reason=ErrorCode.DEADLINE_EXCEEDED
        )
    with pytest.raises(IllegalTransition):
        async with repo.db.transaction() as tx:
            await repo.transition_task(
                tx, await _current(repo), T.PENDING, now=now, fields={"attempt": 3}
            )
    assert (await _current(repo)).attempt == 2


async def test_pending_refuses_skipping_an_attempt(repo: Repository) -> None:
    task = await _seed_task(repo)  # attempt 1
    async with repo.db.transaction() as tx:
        await repo.transition_task(
            tx, task, T.TIMED_OUT, now=START, reason=ErrorCode.DEADLINE_EXCEEDED
        )
    with pytest.raises(IllegalTransition):
        async with repo.db.transaction() as tx:
            await repo.transition_task(
                tx, await _current(repo), T.PENDING, now=START, fields={"attempt": 3}
            )
    current = await _current(repo)
    assert current.status is T.TIMED_OUT and current.attempt == 1
