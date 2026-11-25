"""Agent-internal LLM output model (S2 spec §8.2). Not a wire contract.

Strict-schema compatible: every field required, no extra fields, and no reliance on
``minimum``/``maxLength`` keywords. The rules the schema does not carry are checked after parsing
by :func:`check_output` (ids exactly ``C1..Cn``, confidence in [0, 1], length caps); any violation
triggers the single repair call.
"""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "sigint-llm-output@1.0.0"
STATEMENT_MAX = 300
REASONING_MAX = 500


class PatternLabel(StrEnum):
    RECURRING_CONTACT = "RECURRING_CONTACT"
    ISOLATED_CONTACT = "ISOLATED_CONTACT"
    INCONCLUSIVE = "INCONCLUSIVE"


class CandidateClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    pattern_label: PatternLabel
    confidence: float
    statement: str
    reasoning_summary: str


class SigintLlmOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classifications: list[CandidateClassification]


def check_output(output: SigintLlmOutput, candidate_ids: list[str]) -> list[str]:
    """Post-parse rules. Returns content-free violation messages (empty list = valid)."""
    errors: list[str] = []
    seen = [c.candidate_id for c in output.classifications]
    expected = set(candidate_ids)
    missing = sorted(expected - set(seen), key=_alias_order)
    unknown = sorted({s for s in seen if s not in expected}, key=_alias_order)
    dupes = sorted({s for s in seen if seen.count(s) > 1}, key=_alias_order)
    if missing:
        errors.append(f"missing candidate_id(s): {', '.join(missing)}")
    if unknown:
        errors.append(f"unknown candidate_id(s) (only {_range(candidate_ids)} exist)")
    if dupes:
        errors.append(f"duplicate candidate_id(s): {', '.join(dupes)}")
    for c in output.classifications:
        if c.candidate_id not in expected:
            continue
        where = c.candidate_id
        if not math.isfinite(c.confidence) or not 0.0 <= c.confidence <= 1.0:
            errors.append(f"{where}.confidence must be within [0, 1]")
        if not c.statement.strip():
            errors.append(f"{where}.statement must not be empty")
        elif len(c.statement) > STATEMENT_MAX:
            errors.append(f"{where}.statement exceeds {STATEMENT_MAX} characters")
        if not c.reasoning_summary.strip():
            errors.append(f"{where}.reasoning_summary must not be empty")
        elif len(c.reasoning_summary) > REASONING_MAX:
            errors.append(f"{where}.reasoning_summary exceeds {REASONING_MAX} characters")
    return errors


def _alias_order(alias: str) -> tuple[int, str]:
    digits = alias[1:]
    return (int(digits), alias) if alias.startswith("C") and digits.isdigit() else (10**9, alias)


def _range(ids: list[str]) -> str:
    return f"{ids[0]}..{ids[-1]}" if ids else "none"
