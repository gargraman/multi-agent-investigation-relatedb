from nexus_contracts.common import AgentId, Classification, EntityType


def test_classification_rank_order() -> None:
    assert [c.rank for c in Classification] == [1, 2, 3]
    assert Classification("DEMO-MID").rank == 2


def test_wire_values() -> None:
    assert AgentId.SIGINT == "sigint-agent"
    assert EntityType.LOCATION.value == "LOCATION"
