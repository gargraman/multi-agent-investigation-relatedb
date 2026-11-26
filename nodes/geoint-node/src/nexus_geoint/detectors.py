"""Deterministic GEOINT detectors (spec §6.3, S3a FR-112..FR-116).

Plain version: snap every sighting to its grid cell, then connect two sightings with an edge when
they overlap in time and either (a) belong to different subjects within one neighbour radius
(CO_LOCATION) or (b) belong to the same subject farther apart than that radius (CONFLICT, one
subject cannot be in two places). Each connected component of that edge graph is split by cell and
every cell group is one placement candidate. Everything here is a graph fact; the LLM never adds,
drops or edits candidates.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from nexus_contracts.common import EntityType
from nexus_contracts.scenario.fixtures import SightingLog
from nexus_contracts.scenario.manifest import AgentManifest

_SUBJECT_TYPES = frozenset({EntityType.PERSON, EntityType.ASSET})


class Pattern(StrEnum):
    CO_LOCATION = "CO_LOCATION"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class Sighting:
    evidence_id: str
    observation_id: str
    subject: str  # resolved PERSON/ASSET manifest id
    cell: tuple[int, int]  # (col, row)
    location: str  # LOC-GRID-nn
    start: datetime
    end: datetime  # half-open [start, end)


@dataclass(frozen=True, slots=True)
class PlacementCandidate:
    location: str
    cell: tuple[int, int]
    sightings: tuple[Sighting, ...]  # sorted by (subject, start, observation_id)
    patterns: frozenset[Pattern]

    @property
    def about(self) -> list[str]:
        return sorted({s.subject for s in self.sightings}) + [self.location]

    @property
    def evidence_ids(self) -> list[str]:
        return sorted({s.evidence_id for s in self.sightings})

    @property
    def valid_from(self) -> datetime:
        return min(s.start for s in self.sightings)

    @property
    def valid_until(self) -> datetime:
        return max(s.end for s in self.sightings)


@dataclass(frozen=True, slots=True)
class Resolution:
    sightings: list[Sighting]
    unresolved: int  # subject is not a known PERSON/ASSET
    off_grid: int  # point outside the grid, or the cell has no LOCATION entity


def resolve_sightings(manifest: AgentManifest, logs: Mapping[str, SightingLog]) -> Resolution:
    """Resolve subjects (exact id/alias, PERSON or ASSET only) and snap points to grid cells.
    Dropped observations are counted, never silently lost."""
    index = manifest.alias_index()
    sightings: list[Sighting] = []
    unresolved = 0
    off_grid = 0
    for evidence_id in sorted(logs):
        for obs in logs[evidence_id].observations:
            entity_id = index.get(obs.subject)
            if entity_id is None or manifest.entity(entity_id).type not in _SUBJECT_TYPES:
                unresolved += 1
                continue
            cell = manifest.grid.cell_of(obs.x, obs.y)
            location = None if cell is None else manifest.location_at_cell(*cell)
            if cell is None or location is None:
                off_grid += 1
                continue
            sightings.append(
                Sighting(
                    evidence_id=evidence_id,
                    observation_id=obs.observation_id,
                    subject=entity_id,
                    cell=cell,
                    location=location,
                    start=obs.observed_from,
                    end=obs.observed_until,
                )
            )
    return Resolution(sightings=sightings, unresolved=unresolved, off_grid=off_grid)


def _overlap(a: Sighting, b: Sighting) -> bool:
    return a.start < b.end and b.start < a.end


def placement_candidates(
    manifest: AgentManifest, sightings: Sequence[Sighting]
) -> list[PlacementCandidate]:
    """One candidate per (connected component, cell), sorted by (location, valid_from, observation
    ids). A sighting with no edge produces nothing."""
    grid = manifest.grid
    radius = grid.neighbour_radius
    centres = {s.cell: grid.cell_centre(*s.cell) for s in sightings}

    parent = list(range(len(sightings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    edges: list[tuple[int, int, Pattern]] = []
    for i, a in enumerate(sightings):
        for j in range(i + 1, len(sightings)):
            b = sightings[j]
            if not _overlap(a, b):
                continue
            ca, cb = centres[a.cell], centres[b.cell]
            near = math.hypot(ca.x - cb.x, ca.y - cb.y) <= radius
            if a.subject != b.subject and near:
                pattern = Pattern.CO_LOCATION
            elif a.subject == b.subject and not near:
                pattern = Pattern.CONFLICT
            else:
                continue
            edges.append((i, j, pattern))
            parent[find(i)] = find(j)

    # Components are the union-find classes of sightings touched by at least one edge. A cell group
    # takes every component sighting in that cell, so a conflict endpoint still yields its own
    # candidate even though its edges lead to another cell.
    by_root: dict[int, list[int]] = defaultdict(list)
    patterns_of: dict[int, set[Pattern]] = defaultdict(set)
    for i, j, pattern in edges:
        patterns_of[i].add(pattern)
        patterns_of[j].add(pattern)
    for i in sorted(patterns_of):
        by_root[find(i)].append(i)

    candidates: list[PlacementCandidate] = []
    for idxs in by_root.values():
        by_cell: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i in idxs:
            by_cell[sightings[i].cell].append(i)
        for cell, cell_idxs in by_cell.items():
            ordered = tuple(
                sorted(
                    (sightings[i] for i in cell_idxs),
                    key=lambda s: (s.subject, s.start, s.observation_id),
                )
            )
            patterns: set[Pattern] = set()
            for i in cell_idxs:
                patterns |= patterns_of[i]
            candidates.append(
                PlacementCandidate(
                    location=ordered[0].location,
                    cell=cell,
                    sightings=ordered,
                    patterns=frozenset(patterns),
                )
            )
    # The observation-id tiebreak makes the order a pure function of the sighting set, whatever
    # order the sightings arrived in.
    return sorted(
        candidates,
        key=lambda c: (c.location, c.valid_from, tuple(s.observation_id for s in c.sightings)),
    )
