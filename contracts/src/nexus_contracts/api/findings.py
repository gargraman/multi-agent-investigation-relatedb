"""``POST /v1/findings:batch`` (spec §8.5, §9.2, §9.3, §12.3; ADR-007).

All findings of one task travel in one batch, idempotent on ``task_id``. Each finding carries
evidence refs, ``ABOUT`` entity manifest ids, a half-open validity window and typed assertions.

**Local (model-level) validation, no graph needed:**

- every window is half-open and non-empty: ``valid_from < valid_until`` (§9.3, ADR-007 G7);
- every assertion window lies inside its finding window (§9.3, G7/G10);
- ``LINKED_TO``: ``subject != object`` (G10);
- every assertion subject is one of the finding's ``about`` ids (G10);
- ``LINKED_TO`` with ``link_type: CONTACT`` is **normalised** to canonical order (G3): the lower
  ``manifest_id`` (Python/Cypher string order) becomes ``subject``. Normalising (not rejecting)
  makes the parsed batch, and therefore its ``content_hash``, independent of detector pair order.
- all datetimes are UTC (``UtcDatetime``).

**Left to the Context Service (needs the graph or caller identity):** entities/location exist in
*this* investigation (``HAS_ENTITY``); entity types match the who-asserts-what rule (SIGINT
``LINKED_TO`` HANDLE↔HANDLE, GEOINT ``PLACED_AT`` PERSON|ASSET at LOCATION); ``AT`` targets a
``Location``; caller policy/clearance; evidence ownership. Server-set provenance (``id``,
``source_agent_id``/``source_node_id`` from the caller token, ``created_at``, ``status``,
``version``) is not accepted from agents.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from nexus_contracts.common import (
    AgentId,
    Attempt,
    Classification,
    Confidence,
    ConfidenceBand,
    FindingId,
    FindingKind,
    InvestigationId,
    ManifestId,
    Sha256Hex,
    ShortText,
    TaskId,
    UtcDatetime,
    ValueModel,
)


class LinkType(StrEnum):
    """``LINKED_TO.link_type`` (§9.3). Only ``CONTACT`` is specified; it is symmetric (G3)."""

    CONTACT = "CONTACT"


SYMMETRIC_LINK_TYPES: frozenset[LinkType] = frozenset({LinkType.CONTACT})


class _Window(ValueModel):
    valid_from: UtcDatetime
    valid_until: UtcDatetime

    @model_validator(mode="after")
    def _half_open(self) -> _Window:
        if not self.valid_from < self.valid_until:
            raise ValueError(
                "window must satisfy valid_from < valid_until (half-open [from, until))"
            )
        return self

    def within(self, outer: _Window) -> bool:
        return outer.valid_from <= self.valid_from and self.valid_until <= outer.valid_until


class LinkedToAssertion(_Window):
    """Entity ↔ entity link in a window (SIGINT; §9.3, ADR-007 §1)."""

    type: Literal["LINKED_TO"] = "LINKED_TO"
    subject: ManifestId
    object: ManifestId
    link_type: LinkType

    @model_validator(mode="before")
    @classmethod
    def _canonical_order(cls, data: Any) -> Any:
        if isinstance(data, dict):
            s, o, lt = data.get("subject"), data.get("object"), data.get("link_type")
            if (
                isinstance(s, str)
                and isinstance(o, str)
                and lt in {t.value for t in SYMMETRIC_LINK_TYPES}
                and o < s
            ):
                data = {**data, "subject": o, "object": s}
        return data

    @model_validator(mode="after")
    def _distinct(self) -> LinkedToAssertion:
        if self.subject == self.object:
            raise ValueError("LINKED_TO subject and object must differ")
        return self

    def sort_key(self) -> tuple[str, str, str, datetime]:
        return (self.type, self.subject, self.object, self.valid_from)


class PlacedAtAssertion(_Window):
    """Entity at a LOCATION grid cell in a window (GEOINT; §9.3, ADR-007 §1)."""

    type: Literal["PLACED_AT"] = "PLACED_AT"
    subject: ManifestId
    location: ManifestId

    def sort_key(self) -> tuple[str, str, str, datetime]:
        return (self.type, self.subject, self.location, self.valid_from)


Assertion = Annotated[LinkedToAssertion | PlacedAtAssertion, Field(discriminator="type")]


def canonical_link_pair(a: str, b: str) -> tuple[str, str]:
    """G3 helper: ``(subject, object)`` for a symmetric link (lower manifest id first)."""
    return (a, b) if a <= b else (b, a)


class EvidenceRefSubmission(ValueModel):
    """Evidence reference sent with a finding: id, hash, owner, short summary (§11.3 step 5).
    Never raw content (§9.1)."""

    evidence_id: ManifestId
    sha256: Sha256Hex
    owner_node_id: AgentId
    summary: str = Field(min_length=1, max_length=500)


class FindingDraft(_Window):
    """One finding as submitted by an agent (§9.2 agent-supplied fields + ADR-007 assertions).

    ``statement`` is an addition: §9.2 lists provenance fields only and names no field for the
    finding's own content.
    """

    kind: FindingKind
    statement: ShortText
    confidence: Confidence
    confidence_band: ConfidenceBand
    classification: Classification
    about: list[ManifestId] = Field(min_length=1, description="ABOUT entity manifest ids.")
    evidence: list[EvidenceRefSubmission] = Field(min_length=1)
    assertions: list[Assertion] = Field(default_factory=list)
    reasoning_summary: ShortText
    model_id: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=200)
    schema_version: str = Field(min_length=1, max_length=64)
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def _local_rules(self) -> FindingDraft:
        if len(set(self.about)) != len(self.about):
            raise ValueError("about must not contain duplicates")
        ids = [e.evidence_id for e in self.evidence]
        if len(set(ids)) != len(ids):
            raise ValueError("evidence must not repeat an evidence_id")
        about = set(self.about)
        for i, a in enumerate(self.assertions):
            if a.subject not in about:
                raise ValueError(f"assertions[{i}]: subject {a.subject!r} is not in about")
            if not a.within(self):
                raise ValueError(f"assertions[{i}]: window must lie inside the finding window")
        return self


class FindingBatchRequest(ValueModel):
    """``POST /v1/findings:batch`` body. Idempotency key = ``task_id`` (§8.5)."""

    task_id: TaskId
    investigation_id: InvestigationId
    attempt: Attempt
    findings: list[FindingDraft] = Field(min_length=1)

    def content_hash(self) -> str:
        """sha256 of the canonical JSON of the batch (§8.5, v1.3): sorted keys, no whitespace,
        UTF-8; ``confidence`` rounded to 4 decimals; assertions sorted by (type, subject,
        object/location, valid_from); every other list inside a finding sorted by its canonical
        JSON; findings sorted by their own canonical JSON. Order therefore never changes the
        hash, so a reordered retry is not a false ``409``. ``task_id``, ``attempt`` and
        ``investigation_id`` are excluded so attempts of one task compare equal on identical
        content. (The response still returns finding ids in submission order.)"""

        def canon(obj: object) -> str:
            return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        findings = []
        for f in self.findings:
            body = f.model_dump(mode="json")
            body["confidence"] = round(f.confidence, 4)
            for key, value in body.items():
                if isinstance(value, list) and key != "assertions":
                    body[key] = sorted(value, key=canon)
            body["assertions"] = [
                a.model_dump(mode="json") for a in sorted(f.assertions, key=lambda a: a.sort_key())
            ]
            findings.append(canon(body))
        raw = "[" + ",".join(sorted(findings)) + "]"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class FindingBatchResponse(ValueModel):
    """Success body: committed finding ids, in submission order (§8.5). A replay with the same
    hash returns the original ids; a different hash gets ``409 already_committed`` as
    ``ProblemDetails`` carrying the committed ``finding_ids`` as an extension member."""

    task_id: TaskId
    finding_ids: list[FindingId] = Field(min_length=1)
