"""An agent spec's clearance (agent card) must equal the Context Service policy write clearance."""

import pytest

from nexus_context_service.policy import Policy
from nexus_geoint.spec import GEOINT
from nexus_sigint.spec import SIGINT


@pytest.mark.parametrize("spec", [SIGINT, GEOINT], ids=lambda s: s.agent_id.value)
def test_spec_clearance_matches_context_service_table(spec) -> None:
    assert spec.clearance == Policy.load(None).write_clearance(spec.agent_id.value)
