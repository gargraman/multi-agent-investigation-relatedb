"""Replay latency knob (S3b FR-14a): a fixed delay after a replay hit, for `make chaos` only."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from nexus_contracts.a2a.task import TaskUsage
from nexus_shared.agent.app import build_provider
from nexus_shared.agent.settings import AgentSettings
from nexus_shared.llm import (
    LLMResult,
    RecordingProvider,
    ReplayKeyContext,
    ReplayLLMProvider,
    ReplayMissError,
    TokenBudget,
)

CTX = ReplayKeyContext("baseline-corroboration", "sigint-agent", "classify", "out@1.0.0")
BASE = {"service_name": "test-agent", "service_token": "s" * 40, "sqlite_path": "var/t.sqlite"}


class Out(BaseModel):
    label: str


class Scripted:
    async def generate_structured(self, **kw: Any) -> LLMResult:
        usage = TaskUsage(input_tokens=10, output_tokens=5, llm_calls=1)
        return LLMResult(output=Out(label="ok"), usage=usage, model_id="m-1", latency_ms=1)


def _kwargs() -> dict[str, Any]:
    return {
        "task": "classify",
        "instructions": "be good",
        "prompt_version": "p@1",
        "prompt_sha256": "a" * 64,
        "input_data": {"x": 1},
        "output_schema": Out,
        "budget": TokenBudget(1000),
        "call_ordinal": 0,
        "key_context": CTX,
    }


@pytest.fixture
async def recorded(tmp_path: Path) -> Path:
    recorder = RecordingProvider(Scripted())
    await recorder.generate_structured(**_kwargs())
    recorder.write(tmp_path)
    return tmp_path


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *a: Any, **k: Any) -> None:
        calls.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return calls


async def test_latency_zero_default_no_sleep(recorded: Path, sleeps: list[float]) -> None:
    await ReplayLLMProvider(recorded).generate_structured(**_kwargs())
    assert sleeps == []


async def test_latency_applied_on_hit(recorded: Path, sleeps: list[float]) -> None:
    await ReplayLLMProvider(recorded, latency_ms=500).generate_structured(**_kwargs())
    assert sleeps == [0.5]


async def test_latency_not_applied_on_miss(tmp_path: Path, sleeps: list[float]) -> None:
    with pytest.raises(ReplayMissError):
        await ReplayLLMProvider(tmp_path, latency_ms=500).generate_structured(**_kwargs())
    assert sleeps == []


async def test_replay_output_identical_with_latency(recorded: Path, sleeps: list[float]) -> None:
    fast = await ReplayLLMProvider(recorded).generate_structured(**_kwargs())
    slow = await ReplayLLMProvider(recorded, latency_ms=500).generate_structured(**_kwargs())
    assert slow.output == fast.output
    assert slow.usage == fast.usage
    assert slow.model_id == fast.model_id
    assert sleeps == [0.5]


def test_latency_refused_in_live_mode() -> None:
    with pytest.raises(ValidationError, match="REPLAY_LATENCY"):
        AgentSettings(**BASE, nexus_llm_mode="live", openai_api_key="k", replay_latency_ms=1)
    with pytest.raises(ValidationError):
        AgentSettings(**BASE, nexus_llm_mode="replay", replay_latency_ms=-1)


def test_latency_allowed_in_replay_mode_and_reaches_the_provider() -> None:
    settings = AgentSettings(**BASE, nexus_llm_mode="replay", replay_latency_ms=250)
    provider = build_provider(settings)
    assert isinstance(provider, ReplayLLMProvider) and provider.latency_ms == 250


def test_latency_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_REPLAY_LATENCY_MS", "20000")
    assert AgentSettings(**BASE, nexus_llm_mode="replay").replay_latency_ms == 20000
