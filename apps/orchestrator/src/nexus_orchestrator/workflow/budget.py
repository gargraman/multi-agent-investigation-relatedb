"""Analysis token budget check (S3b FR-128; main spec §8.5, G4).

Plain version: the investigation has a token cap. Specialists spend from it. Before the analysis
agent could start there must still be ``NEXUS_MIN_ANALYSIS_TOKENS`` (default 8000) left, or the
investigation should end ``PARTIAL(budget_exceeded)``. Analysis is not enabled in S3, so the
``Poller`` only logs the answer; S4 turns it into a status change.
"""

from __future__ import annotations

from collections.abc import Iterable

from nexus_orchestrator.state.repository import TaskRecord


def analysis_budget_short(cap: int, used: int, min_analysis: int) -> bool:
    """True when what is left of ``cap`` is below the analysis minimum."""
    return cap - used < min_analysis


def tokens_used(tasks: Iterable[TaskRecord]) -> int:
    """Σ of the known per-task usage (input + output); an unknown usage counts as 0."""
    return sum(t.usage_tokens or 0 for t in tasks)
