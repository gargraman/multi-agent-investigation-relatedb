"""Confidence-band thresholds shared by every agent pipeline (G7)."""

from __future__ import annotations

import pytest

from nexus_contracts.common import ConfidenceBand
from nexus_shared.agent.bands import band_for


@pytest.mark.parametrize(
    ("confidence", "band"),
    [
        (0.0, ConfidenceBand.LOW),
        (0.39, ConfidenceBand.LOW),
        (0.40, ConfidenceBand.MODERATE),
        (0.749, ConfidenceBand.MODERATE),
        (0.75, ConfidenceBand.HIGH),
        (1.0, ConfidenceBand.HIGH),
    ],
)
def test_band_for_edges(confidence: float, band: ConfidenceBand) -> None:
    assert band_for(confidence) is band
