"""S4a contract additions: search, provenance, graph-value reads, retraction event."""

import pytest
from pydantic import ValidationError

from nexus_contracts.api.context import InvestigationContext, SearchRequest
from nexus_contracts.api.graph import ContradictionPair, HiddenLinkWindow
from nexus_contracts.events.timeline import TimelineEventType
from nexus_contracts.registry import SCHEMA_MODELS


def test_search_request_defaults() -> None:
    req = SearchRequest(investigation_id="inv_test1")
    assert req.limit == 200
    assert req.entity_ids == [] and req.kinds == [] and req.statuses == []
    for bad in (0, 501):
        with pytest.raises(ValidationError):
            SearchRequest(investigation_id="inv_test1", limit=bad)


def test_contradiction_pair_is_ordered() -> None:
    assert ContradictionPair(finding_a="fnd_a", finding_b="fnd_b").finding_a == "fnd_a"
    with pytest.raises(ValidationError):
        ContradictionPair(finding_a="fnd_b", finding_b="fnd_a")
    with pytest.raises(ValidationError):
        ContradictionPair(finding_a="fnd_a", finding_b="fnd_a")


def test_hidden_link_window_round_trips_from_alias() -> None:
    w = HiddenLinkWindow.model_validate(
        {
            "cell_c": "CELL-C",
            "cell_b": "CELL-B",
            "from": "2026-01-01T11:00:00Z",
            "until": "2026-01-01T12:00:00Z",
        }
    )
    dumped = w.model_dump(by_alias=True, mode="json")
    assert "from" in dumped and "from_" not in dumped
    assert HiddenLinkWindow.model_validate(dumped) == w


def test_context_assessment_defaults_none() -> None:
    ctx = InvestigationContext.model_validate(
        {"investigation_id": "inv_test1", "status": "RUNNING", "findings": []}
    )
    assert ctx.assessment is None


def test_timeline_has_evidence_retracted() -> None:
    assert TimelineEventType("evidence.retracted") is TimelineEventType.EVIDENCE_RETRACTED


def test_new_models_are_registered() -> None:
    for name in (
        "search-request",
        "search-response",
        "provenance",
        "contradiction-pair",
        "hidden-link",
        "graph",
        "evidence-retracted-payload",
    ):
        assert name in SCHEMA_MODELS
