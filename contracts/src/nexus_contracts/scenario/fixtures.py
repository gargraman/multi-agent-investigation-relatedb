"""Evidence fixture models: ``data/fixtures/<node>/<scenario>/<evidence_id>.json``.

One file is one evidence item. Shapes are chosen for the deterministic detectors (spec §6.3):

- SIGINT ``CommsLog``: communication *metadata* between fictional handles (who, whom, when,
  how long). The detector finds recurring contact pairs and their time windows -> ``LINKED_TO``.
- GEOINT ``SightingLog``: raw cartesian points with a half-open time window. The detector snaps
  each point to its grid cell -> ``PLACED_AT``, and finds co-location windows.

Mentions (handles, subjects) are free strings resolved against manifest ids/aliases (spec §11.4);
unresolvable mentions become UNRESOLVED entities. All times are UTC, windows half-open
``[from, until)`` (ADR-007 G7). See ``data/fixtures/README.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from nexus_contracts.common import Classification

SIGINT_SCHEMA = "nexus.fixture.sigint.comms/1"
GEOINT_SCHEMA = "nexus.fixture.geoint.sightings/1"


def _utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must be UTC (offset +00:00 / 'Z')")
    return value


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_utc)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _FixtureHeader(_Model):
    evidence_id: str
    synthetic: Literal[True]
    classification: Classification
    summary: str = Field(min_length=1, max_length=280, description="Short, shareable summary.")


class CommsRecord(_Model):
    """One synthetic contact event between two handles. Window = [start, start + duration_s)."""

    record_id: str
    channel: Literal["VOICE", "TEXT", "DATA"]
    from_handle: str = Field(min_length=1)
    to_handle: str = Field(min_length=1)
    start: UtcDatetime
    duration_s: int = Field(gt=0)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_s)

    @model_validator(mode="after")
    def _two_parties(self) -> CommsRecord:
        if self.from_handle == self.to_handle:
            raise ValueError(f"{self.record_id}: a contact needs two different handles")
        return self


class CommsLog(_FixtureHeader):
    """SIGINT evidence fixture."""

    schema_id: Literal["nexus.fixture.sigint.comms/1"] = Field(alias="schema")
    collector: str = Field(min_length=1)
    records: list[CommsRecord] = Field(min_length=1)


class Observation(_Model):
    """One synthetic sighting of a subject at a raw point, in [observed_from, observed_until)."""

    observation_id: str
    subject: str = Field(min_length=1)
    x: float
    y: float
    observed_from: UtcDatetime
    observed_until: UtcDatetime

    @model_validator(mode="after")
    def _window(self) -> Observation:
        if self.observed_until <= self.observed_from:
            raise ValueError(f"{self.observation_id}: observed_until must be after observed_from")
        return self


class SightingLog(_FixtureHeader):
    """GEOINT evidence fixture."""

    schema_id: Literal["nexus.fixture.geoint.sightings/1"] = Field(alias="schema")
    sensor: str = Field(min_length=1)
    observations: list[Observation] = Field(min_length=1)


Fixture = CommsLog | SightingLog
