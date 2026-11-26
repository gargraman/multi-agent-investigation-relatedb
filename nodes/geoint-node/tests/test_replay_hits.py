"""Guard: the committed GEOINT recordings hit under replay (FR-142). The SIGINT ``contradiction``
case is parametrized into ``nodes/sigint-node/tests/test_replay_hits.py``."""

from __future__ import annotations

import pytest

from nexus_contracts.a2a.task import AgentTaskStatus
from nexus_contracts.api.findings import FindingBatchRequest
from nexus_geoint.spec import GEOINT
from nexus_shared.llm import ReplayLLMProvider, TokenBudget

from .conftest import FIXTURES, ROOT, make_request, manifest_for

SCENARIOS = ["baseline-corroboration", "contradiction", "prompt-injection"]


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_committed_geoint_recordings_hit(scenario: str) -> None:
    request = make_request(scenario)
    outcome = await GEOINT.run(
        request,
        provider=ReplayLLMProvider(ROOT / "data/replay"),
        manifest=manifest_for(scenario),
        fixtures_root=FIXTURES,
        budget=TokenBudget(request.token_budget),
    )
    assert outcome.status is AgentTaskStatus.COMPLETED, outcome.error
    assert outcome.usage.replayed is True
    assert isinstance(outcome.batch, FindingBatchRequest)
    assert outcome.batch.findings
