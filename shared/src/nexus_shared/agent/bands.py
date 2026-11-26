"""Confidence bands shared by every agent pipeline (G7, confirmed): LOW < 0.40 <= MODERATE < 0.75
<= HIGH."""

from __future__ import annotations

from nexus_contracts.common import ConfidenceBand

BAND_MODERATE = 0.40
BAND_HIGH = 0.75

__all__ = ["BAND_HIGH", "BAND_MODERATE", "band_for"]


def band_for(confidence: float) -> ConfidenceBand:
    if confidence < BAND_MODERATE:
        return ConfidenceBand.LOW
    if confidence < BAND_HIGH:
        return ConfidenceBand.MODERATE
    return ConfidenceBand.HIGH
