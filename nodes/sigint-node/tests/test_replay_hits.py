"""Guard: the committed SIGINT recordings still hit (S2 key material unchanged; S3a added
``contradiction``)."""

from __future__ import annotations

import pytest

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_shared.llm import ReplayLLMProvider, TokenBudget
from nexus_sigint.spec import SIGINT

from .conftest import FIXTURES, ROOT, make_request, manifest_for


@pytest.mark.parametrize(
    "scenario", ["baseline-corroboration", "contradiction", "prompt-injection"]
)
async def test_committed_sigint_recordings_hit(scenario: str) -> None:
    request = make_request(scenario)
    outcome = await SIGINT.run(
        request,
        provider=ReplayLLMProvider(ROOT / "data/replay"),
        manifest=manifest_for(scenario),
        fixtures_root=FIXTURES,
        budget=TokenBudget(request.token_budget),
    )
    assert outcome.status is AgentTaskStatus.COMPLETED, outcome.error
    assert outcome.usage.replayed is True
    assert outcome.batch is not None
