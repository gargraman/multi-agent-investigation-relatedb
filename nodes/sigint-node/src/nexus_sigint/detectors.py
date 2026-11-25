"""Deterministic SIGINT detectors (spec §6.3, S2 FR-026, G6).

Plain version: read every contact record, turn both handle strings into manifest entity ids
(exact id or alias match), keep only HANDLE↔HANDLE records, group them by the unordered pair and
call a pair a candidate when it has at least two records. Everything here is a graph fact; the
LLM never adds, drops or edits candidates (AC17).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from nexus_contracts.api.findings import canonical_link_pair
from nexus_contracts.common import EntityType
from nexus_contracts.scenario.fixtures import CommsLog
from nexus_contracts.scenario.manifest import ScenarioManifest

MIN_CONTACTS = 2
"""G6: "recurring" = at least two contact records per canonical pair across the task's evidence."""


@dataclass(frozen=True, slots=True)
class ContactRecord:
    evidence_id: str
    record_id: str
    channel: str
    start: datetime
    end: datetime  # start + duration_s, half-open [start, end)
    duration_s: int


@dataclass(frozen=True, slots=True)
class ContactCandidate:
    subject: str  # lower manifest id (G3 canonical order)
    object: str
    contacts: tuple[ContactRecord, ...]  # sorted by (start, evidence_id, record_id)

    @property
    def evidence_ids(self) -> list[str]:
        return sorted({c.evidence_id for c in self.contacts})

    @property
    def valid_from(self) -> datetime:
        return min(c.start for c in self.contacts)

    @property
    def valid_until(self) -> datetime:
        return max(c.end for c in self.contacts)


def resolve_handle(
    mention: str, manifest: ScenarioManifest, index: Mapping[str, str]
) -> str | None:
    """Exact id/alias match (spec §11.4 step 1) to a HANDLE entity, else ``None``."""
    entity_id = index.get(mention)
    if entity_id is None or manifest.entity(entity_id).type is not EntityType.HANDLE:
        return None
    return entity_id


def recurring_contacts(
    manifest: ScenarioManifest,
    evidence: Mapping[str, CommsLog],
    *,
    min_contacts: int = MIN_CONTACTS,
) -> list[ContactCandidate]:
    """Candidates sorted by canonical pair. Unresolved or non-HANDLE mentions are ignored (G8
    parks UNRESOLVED entity creation)."""
    index = manifest.alias_index()
    groups: dict[tuple[str, str], list[ContactRecord]] = defaultdict(list)
    for evidence_id, log in evidence.items():
        for rec in log.records:
            a = resolve_handle(rec.from_handle, manifest, index)
            b = resolve_handle(rec.to_handle, manifest, index)
            if a is None or b is None or a == b:
                continue
            groups[canonical_link_pair(a, b)].append(
                ContactRecord(
                    evidence_id=evidence_id,
                    record_id=rec.record_id,
                    channel=rec.channel,
                    start=rec.start,
                    end=rec.end,
                    duration_s=rec.duration_s,
                )
            )
    candidates = []
    for (subject, obj), records in sorted(groups.items()):
        if len(records) < min_contacts:
            continue
        ordered = tuple(sorted(records, key=lambda r: (r.start, r.evidence_id, r.record_id)))
        candidates.append(ContactCandidate(subject=subject, object=obj, contacts=ordered))
    return candidates
