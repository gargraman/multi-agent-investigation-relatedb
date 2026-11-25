"""Live OpenAI test (FR-052). Skipped unless ``NEXUS_LIVE_LLM_TESTS=1`` and ``OPENAI_API_KEY`` are
set. Makes one or two real calls against the baseline scenario and checks the oracle pairs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_shared.llm import TokenBudget
from nexus_sigint.pipeline import run

from .conftest import FIXTURES, make_request, manifest_for

pytestmark = pytest.mark.skipif(
    os.environ.get("NEXUS_LIVE_LLM_TESTS") != "1" or not os.environ.get("OPENAI_API_KEY"),
    reason="live LLM tests need NEXUS_LIVE_LLM_TESTS=1 and OPENAI_API_KEY",
)


async def test_live_baseline_classification() -> None:
    from nexus_shared.llm import OpenAIProvider, build_ssl_context

    ca = os.environ.get("EXTRA_CA_FILE") or os.environ.get("SSL_CERT_FILE")
    provider = OpenAIProvider(
        model=os.environ.get("OPENAI_MODEL_EXTRACT", "gpt-5.4-mini-2026-03-17"),
        api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
        ssl_context=build_ssl_context(Path(ca) if ca else None),
    )
    request = make_request()
    try:
        out = await run(
            request,
            provider=provider,
            manifest=manifest_for(request.scenario_id),
            fixtures_root=FIXTURES,
            budget=TokenBudget(request.token_budget),
        )
    finally:
        await provider.aclose()
    assert out.status is AgentTaskStatus.COMPLETED, out.error
    assert out.batch is not None
    assert [f.about for f in out.batch.findings] == [
        ["HDL-ALPHA-1", "HDL-CHARLIE-1"],
        ["HDL-DELTA-1", "HDL-ECHO-1"],
    ]
    assert out.usage.llm_calls in (1, 2) and not out.usage.replayed
