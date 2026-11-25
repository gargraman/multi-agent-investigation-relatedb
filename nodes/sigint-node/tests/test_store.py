from __future__ import annotations

from pathlib import Path

import pytest

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_contracts.common import LlmMode
from nexus_sigint.settings import SigintSettings
from nexus_sigint.store import SubmitDecision, TaskStore

from .conftest import make_request


async def test_boot_sweep_fails_in_flight_rows(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    store = TaskStore(path)
    assert await store.open() == 0
    _, row = await store.submit(make_request(), llm_mode=LlmMode.REPLAY)
    await store.update("tsk_test1", 1, row.version, status=AgentTaskStatus.WORKING.value)
    await store.submit(make_request(task_id="tsk_test2"), llm_mode=LlmMode.REPLAY)  # SUBMITTED
    await store.close()

    store = TaskStore(path)
    assert await store.open() == 2
    for tid in ("tsk_test1", "tsk_test2"):
        result = (await store.latest(tid)).to_result()
        assert result.status is AgentTaskStatus.FAILED
        assert result.error is not None and result.error.code.value == "agent_restarted"
        assert result.completed_at is not None
    await store.close()


async def test_dedup_decisions(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "s.sqlite")
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
    store = TaskStore(tmp_path / "s.sqlite")
    await store.open()
    _, row = await store.submit(make_request(), llm_mode=LlmMode.LIVE)
    assert await store.update("tsk_test1", 1, row.version, status="WORKING") is not None
    assert await store.update("tsk_test1", 1, row.version, status="COMPLETED") is None
    with pytest.raises(ValueError):
        await store.update("tsk_test1", 1, row.version, request_hash="x")
    await store.close()


def test_live_mode_requires_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        SigintSettings(service_token="s" * 40, nexus_llm_mode="live")
    ok = SigintSettings(service_token="s" * 40, nexus_llm_mode="replay")
    assert ok.fixtures_dir == Path("data/fixtures") and ok.replay_dir == Path("data/replay")
