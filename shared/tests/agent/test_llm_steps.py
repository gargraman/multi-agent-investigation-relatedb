"""Shared post-parse checks and the untrusted-evidence fence (nexus_shared.agent.llm_steps)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from nexus_shared.agent.llm_steps import (
    FENCE_CLOSE,
    FENCE_OPEN,
    check_classifications,
    fence,
)


@dataclass(frozen=True)
class Item:
    candidate_id: str
    confidence: float = 0.5
    statement: str = "A statement."
    reasoning_summary: str = "A reason."


def _check(items: list[Item], ids: tuple[str, ...] = ("C1", "C2")) -> list[str]:
    return check_classifications(items, list(ids), statement_max=10, reasoning_max=20)


def test_valid_items_pass() -> None:
    assert _check([Item("C1", statement="ok"), Item("C2", statement="ok")]) == []


def test_missing_unknown_duplicate_ids() -> None:
    items = [Item("C1", statement="ok"), Item("C1", statement="ok"), Item("C9", statement="ok")]
    assert _check(items) == [
        "missing candidate_id(s): C2",
        "unknown candidate_id(s) (only C1..C2 exist)",
        "duplicate candidate_id(s): C1",
    ]


def test_alias_order_is_numeric() -> None:
    errors = _check([], ids=tuple(f"C{i}" for i in (10, 2, 1)))
    assert errors == ["missing candidate_id(s): C1, C2, C10"]


@pytest.mark.parametrize(
    ("item", "error"),
    [
        (Item("C1", confidence=1.5, statement="ok"), "C1.confidence must be within [0, 1]"),
        (
            Item("C1", confidence=float("nan"), statement="ok"),
            "C1.confidence must be within [0, 1]",
        ),
        (Item("C1", statement=" "), "C1.statement must not be empty"),
        (Item("C1", statement="x" * 11), "C1.statement exceeds 10 characters"),
        (
            Item("C1", statement="ok", reasoning_summary=""),
            "C1.reasoning_summary must not be empty",
        ),
        (
            Item("C1", statement="ok", reasoning_summary="y" * 21),
            "C1.reasoning_summary exceeds 20 characters",
        ),
    ],
)
def test_field_rules(item: Item, error: str) -> None:
    assert _check([item], ids=("C1",)) == [error]


def test_fence_bytes_are_stable() -> None:
    """SIGINT's S2 replay keys hash this exact text: the format must never drift."""
    text = "a <<<b>>> UNTRUSTED_EVIDENCE c"
    assert fence("ev-1", text) == (
        "<<<UNTRUSTED_EVIDENCE id=ev-1\na < < <b> > > UNTRUSTED-TEXT c\nUNTRUSTED_EVIDENCE>>>"
    )
    assert FENCE_OPEN == "<<<UNTRUSTED_EVIDENCE" and FENCE_CLOSE == "UNTRUSTED_EVIDENCE>>>"
