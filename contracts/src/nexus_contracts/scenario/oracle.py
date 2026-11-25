"""Scenario oracle: what a correct run of a scenario must produce (spec §11.3).

Finding IDs are generated per run, so the oracle never names findings. It names stable manifest
IDs instead (entities and evidence, ADR-006 E1): "a finding by this agent, about these entities,
supported by this evidence". Cross-reference checks against the manifest live in
``ScenarioManifest`` (``manifest.py``), because they need the entity and evidence tables.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_contracts.common import AgentId, AssertionType, FindingKind

# Local literals: the finding/assertion vocabulary is owned by the findings contract (spec §9.2,
# §9.3). Kept as literals here so the scenario pack does not depend on that module's layout.
DenialOutcome = Literal["FILTERED", "DENIED"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectedFinding(_Frozen):
    """A finding a correct run must contain, identified by agent, subject and evidence."""

    agent: AgentId
    kind: FindingKind
    assertion: AssertionType | None = Field(
        default=None,
        description="Structured assertion the finding must carry (ADR-007), if any.",
    )
    about: list[str] = Field(min_length=1, description="Manifest entity IDs the finding is ABOUT.")
    evidence: list[str] = Field(
        default_factory=list, description="Manifest evidence IDs the finding must cite."
    )


class EvidencePair(_Frozen):
    """Two evidence items whose findings must be related (CORROBORATES or CONTRADICTS)."""

    about: list[str] = Field(min_length=1, description="Manifest entity IDs both findings concern.")
    evidence: tuple[str, str] = Field(description="The two evidence IDs, one per finding.")
    note: str = Field(default="", description="Human-readable reason, for reviewers only.")

    @model_validator(mode="after")
    def _distinct(self) -> EvidencePair:
        if self.evidence[0] == self.evidence[1]:
            raise ValueError("an evidence pair needs two different evidence items")
        return self


class HiddenPath(_Frozen):
    """An I4 path A -SIGINT-> via -GEOINT-> B with no direct A-B link (ADR-007 G2, G4)."""

    a: str
    via: str
    b: str

    @model_validator(mode="after")
    def _distinct(self) -> HiddenPath:
        if len({self.a, self.via, self.b}) != 3:
            raise ValueError("hidden path endpoints a, via, b must be three different entities")
        return self


class ExpectedDenial(_Frozen):
    """Findings from these evidence items must be withheld from ``agent`` (spec §9.5, AC18)."""

    agent: AgentId
    evidence: list[str] = Field(min_length=1)
    outcome: DenialOutcome


class Oracle(_Frozen):
    """Expected results of a scenario run (spec §11.3)."""

    expected_findings: list[ExpectedFinding] = Field(default_factory=list)
    corroborations: list[EvidencePair] = Field(default_factory=list)
    contradictions: list[EvidencePair] = Field(default_factory=list)
    hidden_paths: list[HiddenPath] = Field(default_factory=list)
    expected_denials: list[ExpectedDenial] = Field(default_factory=list)
    canary_strings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Oracle:
        if len(set(self.hidden_paths)) != len(self.hidden_paths):
            raise ValueError("duplicate hidden path in oracle")
        if len(set(self.canary_strings)) != len(self.canary_strings):
            raise ValueError("duplicate canary string in oracle")
        if any(len(c) < 8 for c in self.canary_strings):
            raise ValueError("canary strings must be at least 8 characters (avoid false hits)")
        return self
