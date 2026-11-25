"""Vocabulary shared by every contract (spec §9.1, §9.3, §12).

Defined once here so agent, orchestrator, Context Service and scenario-pack models agree.
"""

from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)


class Classification(StrEnum):
    """Fictional demo classification levels (spec §9.3)."""

    DEMO_LOW = "DEMO-LOW"
    DEMO_MID = "DEMO-MID"
    DEMO_HIGH = "DEMO-HIGH"

    @property
    def rank(self) -> int:
        """Numeric rank stored as ``classification_rank`` (1 = DEMO-LOW … 3 = DEMO-HIGH)."""
        return ClassificationRank[self.name].value


class ClassificationRank(IntEnum):
    DEMO_LOW = 1
    DEMO_MID = 2
    DEMO_HIGH = 3


class EntityType(StrEnum):
    """Entity types (spec §9.1). ``LOCATION`` entities are fictional grid cells."""

    PERSON = "PERSON"
    HANDLE = "HANDLE"
    ORG = "ORG"
    ASSET = "ASSET"
    LOCATION = "LOCATION"


class AgentId(StrEnum):
    """Stable agent identifiers (spec §12.2 example: ``sigint-agent``)."""

    SIGINT = "sigint-agent"
    GEOINT = "geoint-agent"
    ANALYSIS = "analysis-agent"


# --- S0 step 6 additions: shared wire vocabulary and base types -----------------------------
# (Additions only; the enums above are unchanged.)


class FindingKind(StrEnum):
    """Finding kind (spec §9.1, §9.2): P0 folds "Observation" into Finding."""

    OBSERVATION = "OBSERVATION"
    INFERENCE = "INFERENCE"


class AssertionType(StrEnum):
    """Structured agent facts (ADR-007): SIGINT asserts LINKED_TO, GEOINT asserts PLACED_AT."""

    LINKED_TO = "LINKED_TO"
    PLACED_AT = "PLACED_AT"


class ConfidenceBand(StrEnum):
    """Mandatory confidence band (spec §9.2). Bands are documented, not calibrated."""

    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"


class FindingStatus(StrEnum):
    """Finding/Assessment lifecycle status (spec §9.2, §9.4)."""

    ACTIVE = "ACTIVE"
    DISPUTED = "DISPUTED"
    SUPERSEDED = "SUPERSEDED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class LlmMode(StrEnum):
    """LLM provider mode (spec §13.2/§13.3, ADR-006 E3). Wire values are lower-case."""

    LIVE = "live"
    REPLAY = "replay"


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be UTC (offset +00:00 or 'Z')")
    return value


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_require_utc)]
"""Timezone-aware UTC datetime (spec §9.2 "UTC ISO-8601", ADR-007 G7).

Naive datetimes and non-zero offsets are **rejected**, not normalised: a non-UTC offset on the
wire means a producer bug, and silently converting it would hide that.
"""

MANIFEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
"""Same pattern as ``nexus_contracts.scenario.manifest`` (entity/evidence manifest ids)."""

ManifestId = Annotated[str, StringConstraints(pattern=MANIFEST_ID_PATTERN, max_length=128)]
"""A manifest id (entity or evidence), scoped to one investigation (ADR-007 G5)."""

_ID_SUFFIX = r"[A-Za-z0-9_-]{1,64}$"
InvestigationId = Annotated[str, StringConstraints(pattern=rf"^inv_{_ID_SUFFIX}")]
TaskId = Annotated[str, StringConstraints(pattern=rf"^tsk_{_ID_SUFFIX}")]
FindingId = Annotated[str, StringConstraints(pattern=rf"^fnd_{_ID_SUFFIX}")]
AssessmentId = Annotated[str, StringConstraints(pattern=rf"^asm_{_ID_SUFFIX}")]
"""Generated ids carry the prefixes named in spec §13.2 (``inv_…``, ``tsk_…``, ``fnd_…``) and
§12.4 (``asm_…``). The suffix is opaque (ULID in the examples)."""

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
"""Bounded free text (summaries, statements). LLM text is rendered as plain text (§6.1)."""

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
"""Model-estimated confidence in [0, 1], not statistically calibrated (§9.2, §12.4)."""

Attempt = Annotated[int, Field(ge=1)]
"""Dispatch attempt, starting at 1. The retry limit (two attempts in P0, §8.2) is workflow
policy enforced by the orchestrator engine, not by the wire contract."""


class WireModel(BaseModel):
    """Base for request/response bodies: unknown fields are a bug, so they are rejected."""

    model_config = ConfigDict(extra="forbid")


class ValueModel(BaseModel):
    """Base for immutable value objects on the wire (extra="forbid", frozen)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
