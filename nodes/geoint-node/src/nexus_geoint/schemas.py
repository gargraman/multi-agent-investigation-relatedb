"""Agent-internal LLM output model for GEOINT (S3a FR-117). Not a wire contract.

Strict-schema compatible, shaped like SIGINT's: every field required, no extra fields, no reliance
on ``minimum``/``maxLength`` keywords. The rules the schema does not carry (ids exactly ``C1..Cn``,
confidence in [0, 1], length caps) are checked after parsing by :func:`check_output`, which uses
the rules shared with SIGINT; any violation triggers the single repair call.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from nexus_shared.agent.llm_steps import check_classifications

SCHEMA_VERSION = "geoint-llm-output@1.0.0"
STATEMENT_MAX = 300
REASONING_MAX = 500


class PatternLabel(StrEnum):
    CO_LOCATION = "CO_LOCATION"
    DWELL_OVERLAP = "DWELL_OVERLAP"
    CONFLICTING_PLACEMENT = "CONFLICTING_PLACEMENT"
    INCONCLUSIVE = "INCONCLUSIVE"


class CandidateClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    pattern_label: PatternLabel
    confidence: float
    statement: str
    reasoning_summary: str


class GeointLlmOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classifications: list[CandidateClassification]


def check_output(output: GeointLlmOutput, candidate_ids: list[str]) -> list[str]:
    """Post-parse rules (shared with SIGINT). Returns content-free violation messages (empty list =
    valid)."""
    return check_classifications(
        output.classifications, candidate_ids, STATEMENT_MAX, REASONING_MAX
    )
