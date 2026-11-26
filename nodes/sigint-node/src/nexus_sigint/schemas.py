"""Agent-internal LLM output model (S2 spec §8.2). Not a wire contract.

Strict-schema compatible: every field required, no extra fields, and no reliance on
``minimum``/``maxLength`` keywords. The rules the schema does not carry are checked after parsing
by :func:`check_output` (ids exactly ``C1..Cn``, confidence in [0, 1], length caps); any violation
triggers the single repair call.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from nexus_shared.agent.llm_steps import check_classifications

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
    """Post-parse rules (shared with GEOINT). Returns content-free violation messages (empty list =
    valid)."""
    return check_classifications(
        output.classifications, candidate_ids, STATEMENT_MAX, REASONING_MAX
    )
