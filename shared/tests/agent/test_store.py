from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_contracts.common import AgentId, LlmMode
from nexus_shared.agent.store import SubmitDecision, TaskStore, request_hash

from .conftest import make_request


async def test_boot_sweep_fails_in_flight_rows(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    store = TaskStore(path, AgentId.SIGINT)
    assert await store.open() == 0
    _, row = await store.submit(make_request(), llm_mode=LlmMode.REPLAY)
    await store.update("tsk_test1", 1, row.version, status=AgentTaskStatus.WORKING.value)
    await store.submit(make_request(task_id="tsk_test2"), llm_mode=LlmMode.REPLAY)  # SUBMITTED
    await store.close()

    store = TaskStore(path, AgentId.SIGINT)
    assert await store.open() == 2
    for tid in ("tsk_test1", "tsk_test2"):
        result = (await store.latest(tid)).to_result()
        assert result.status is AgentTaskStatus.FAILED
        assert result.error is not None and result.error.code.value == "agent_restarted"
        assert result.completed_at is not None
    await store.close()


async def test_dedup_decisions(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "s.sqlite", AgentId.SIGINT)
    await store.open()
    req = make_request(attempt=2)
    assert (await store.submit(req, llm_mode=LlmMode.LIVE))[0] is SubmitDecision.NEW
    assert (await store.submit(req, llm_mode=LlmMode.LIVE))[0] is SubmitDecision.DUPLICATE
    # Orchestrator retry: same body, fresh trace_context -> duplicate, not conflict.
    new_trace = {"traceparent": "00-" + "9" * 32 + "-" + "8" * 16 + "-01"}
    retried = make_request(attempt=2, deadline=req.deadline, trace_context=new_trace)
    assert (await store.submit(retried, llm_mode=LlmMode.LIVE))[0] is SubmitDecision.DUPLICATE
    other = make_request(attempt=2, deadline=req.deadline, objective="different objective text")
    assert (await store.submit(other, llm_mode=LlmMode.LIVE))[0] is SubmitDecision.CONFLICT
    assert (await store.submit(make_request(attempt=1), llm_mode=LlmMode.LIVE))[0] is (
        SubmitDecision.STALE
    )
    decision, row = await store.submit(make_request(attempt=3), llm_mode=LlmMode.LIVE)
    assert decision is SubmitDecision.NEW and (await store.latest("tsk_test1"))["attempt"] == 3
    await store.close()


async def test_cas_update_rejects_moved_version(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "s.sqlite", AgentId.SIGINT)
    await store.open()
    _, row = await store.submit(make_request(), llm_mode=LlmMode.LIVE)
    assert await store.update("tsk_test1", 1, row.version, status="WORKING") is not None
    assert await store.update("tsk_test1", 1, row.version, status="COMPLETED") is None
    with pytest.raises(ValueError):
        await store.update("tsk_test1", 1, row.version, request_hash="x")
    await store.close()


async def test_results_carry_the_stores_agent_id(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "g.sqlite", AgentId.GEOINT)
    await store.open()
    _, row = await store.submit(make_request(agent="geoint-agent"), llm_mode=LlmMode.REPLAY)
    assert row.to_result().agent_id == "geoint-agent"
    assert (await store.latest("tsk_test1")).to_result().agent_id is AgentId.GEOINT
    await store.close()


def test_request_hash_ignores_deadline() -> None:
    a = make_request()
    b = a.model_copy(update={"deadline": a.deadline + timedelta(seconds=90)})
    assert request_hash(a) == request_hash(b)


async def test_request_cancel_flags_only_a_non_terminal_latest_attempt(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "s.sqlite", AgentId.SIGINT)
    await store.open()
    assert await store.request_cancel("tsk_nope") is None

    _, row = await store.submit(make_request(), llm_mode=LlmMode.LIVE)
    working = await store.update("tsk_test1", 1, row.version, status="WORKING")
    flagged = await store.request_cancel("tsk_test1")
    assert flagged is not None and flagged.status is AgentTaskStatus.WORKING
    assert await store.is_cancel_requested("tsk_test1", 1)
    # The flag does not bump the version: the runner's in-flight CAS still succeeds.
    assert flagged.version == working.version
    assert await store.request_cancel("tsk_test1") is not None  # idempotent
    done = await store.update("tsk_test1", 1, working.version, status="COMPLETED")
    assert done is not None

    _, other = await store.submit(make_request(task_id="tsk_test2"), llm_mode=LlmMode.LIVE)
    await store.update("tsk_test2", 1, other.version, status="CANCELED")
    terminal = await store.request_cancel("tsk_test2")
    assert terminal is not None and terminal.status is AgentTaskStatus.CANCELED
    assert not await store.is_cancel_requested("tsk_test2", 1)
    assert not await store.is_cancel_requested("tsk_nope", 1)
    await store.close()


async def test_supersede_cancels_only_lower_non_terminal_attempts(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "s.sqlite", AgentId.SIGINT)
    await store.open()
    await store.submit(make_request(attempt=1), llm_mode=LlmMode.LIVE)
    await store.submit(make_request(attempt=2), llm_mode=LlmMode.LIVE)
    assert await store.supersede_lower_attempts("tsk_test1", 2) == [1]
    lower = (await store.get("tsk_test1", 1)).to_result()
    assert lower.status is AgentTaskStatus.CANCELED and lower.completed_at is not None
    assert (await store.get("tsk_test1", 2)).status is AgentTaskStatus.SUBMITTED
    assert await store.supersede_lower_attempts("tsk_test1", 2) == []  # terminal rows untouched
    await store.close()
