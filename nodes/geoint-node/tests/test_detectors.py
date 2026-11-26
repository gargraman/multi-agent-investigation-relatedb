from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from nexus_contracts.scenario.fixtures import SightingLog
from nexus_geoint.detectors import (
    Pattern,
    placement_candidates,
    resolve_sightings,
)

from .conftest import load_logs, manifest_for

BASE = "baseline-corroboration"


def _log(evidence_id: str, *obs: tuple[str, float, float, int, int]) -> SightingLog:
    """Compact log builder: obs = (subject, x, y, from_hour, until_hour) on 2026-10-20."""
    day = (2026, 10, 20)
    observations: list[dict[str, Any]] = [
        {
            "observation_id": f"{evidence_id}-o{i:02d}",
            "subject": subject,
            "x": x,
            "y": y,
            "observed_from": datetime(*day, start, tzinfo=UTC).isoformat(),
            "observed_until": datetime(*day, end, tzinfo=UTC).isoformat(),
        }
        for i, (subject, x, y, start, end) in enumerate(obs, start=1)
    ]
    return SightingLog.model_validate(
        {
            "schema": "nexus.fixture.geoint.sightings/1",
            "evidence_id": evidence_id,
            "classification": "DEMO-LOW",
            "synthetic": True,
            "summary": "unit test",
            "sensor": "unit-sensor",
            "observations": observations,
        }
    )


def _candidates(*obs: tuple[str, float, float, int, int]):
    m = manifest_for(BASE)
    res = resolve_sightings(m, {"ev-geo-900": _log("ev-geo-900", *obs)})
    return res, placement_candidates(m, res.sightings)


@pytest.mark.parametrize(
    "scenario,expected",
    [
        (
            "baseline-corroboration",
            {
                (("ENT-CHARLIE", "LOC-GRID-14"), ("ev-geo-002",)),
                (("ENT-BRAVO", "LOC-GRID-15"), ("ev-geo-007",)),
                (("ENT-DELTA", "ENT-ECHO", "LOC-GRID-04"), ("ev-geo-011",)),
            },
        ),
        (
            "contradiction",
            {
                (("ENT-ALPHA", "LOC-GRID-02"), ("ev-geo-003",)),
                (("ENT-ALPHA", "LOC-GRID-21"), ("ev-geo-008",)),
            },
        ),
        (
            "prompt-injection",
            {(("ENT-CHARLIE", "ENT-FOXTROT", "LOC-GRID-09"), ("ev-geo-005",))},
        ),
    ],
)
def test_candidates_match_oracle(scenario, expected):
    m = manifest_for(scenario)
    res = resolve_sightings(m, load_logs(scenario))
    got = {(tuple(c.about), tuple(c.evidence_ids)) for c in placement_candidates(m, res.sightings)}
    assert got == expected


def test_contradiction_patterns_are_conflict():
    m = manifest_for("contradiction")
    res = resolve_sightings(m, load_logs("contradiction"))
    candidates = placement_candidates(m, res.sightings)
    assert len(candidates) == 2
    assert all(Pattern.CONFLICT in c.patterns for c in candidates)


def test_colocation_candidate_has_colocation_pattern_and_window():
    m = manifest_for("prompt-injection")
    res = resolve_sightings(m, load_logs("prompt-injection"))
    (cand,) = placement_candidates(m, res.sightings)
    assert Pattern.CO_LOCATION in cand.patterns
    assert cand.valid_from == min(s.start for s in cand.sightings)
    assert cand.valid_until == max(s.end for s in cand.sightings)
    assert [s.subject for s in cand.sightings] == sorted(s.subject for s in cand.sightings)


@pytest.mark.parametrize(
    "x2,y2",
    [(50, 50), (150, 50), (150, 150)],
    ids=["same-cell", "neighbour", "diagonal"],
)
def test_same_cell_and_neighbour_and_diagonal_colocate(x2, y2):
    # distance between cell centres: 0, 100, 141.4 -> all <= 150
    _, got = _candidates(
        ("subject-alpha", 50, 50, 8, 10),
        ("subject-bravo", x2, y2, 9, 11),
    )
    assert got
    assert all(c.patterns == {Pattern.CO_LOCATION} for c in got)
    assert {c.location for c in got} <= {"LOC-GRID-00", "LOC-GRID-01", "LOC-GRID-07"}


def test_neighbour_cells_yield_one_candidate_per_cell():
    _, got = _candidates(
        ("subject-alpha", 50, 50, 8, 10),
        ("subject-bravo", 150, 50, 9, 11),
    )
    assert [c.location for c in got] == ["LOC-GRID-00", "LOC-GRID-01"]
    assert [c.about for c in got] == [
        ["ENT-ALPHA", "LOC-GRID-00"],
        ["ENT-BRAVO", "LOC-GRID-01"],
    ]


def test_two_cells_apart_do_not_colocate():
    # distance 200 > 150
    _, got = _candidates(
        ("subject-alpha", 50, 50, 8, 10),
        ("subject-bravo", 250, 50, 8, 10),
    )
    assert got == []


def test_touching_windows_do_not_overlap():
    _, got = _candidates(
        ("subject-alpha", 50, 50, 8, 10),
        ("subject-bravo", 50, 50, 10, 12),
    )
    assert got == []


def test_same_subject_conflict_needs_overlap():
    _, got = _candidates(
        ("subject-alpha", 50, 50, 8, 10),
        ("subject-alpha", 450, 350, 10, 12),
    )
    assert got == []
    _, got = _candidates(
        ("subject-alpha", 50, 50, 8, 10),
        ("subject-alpha", 450, 350, 9, 12),
    )
    assert {c.patterns == frozenset({Pattern.CONFLICT}) for c in got} == {True}
    assert len(got) == 2


def test_same_subject_nearby_is_neither_edge():
    _, got = _candidates(
        ("subject-alpha", 50, 50, 8, 10),
        ("subject-alpha", 150, 50, 8, 10),
    )
    assert got == []


def test_boundary_point_snaps_to_higher_cell_and_off_grid_counted():
    m = manifest_for(BASE)
    log = _log(
        "ev-geo-901",
        ("subject-alpha", 100, 50, 8, 10),  # x == pitch -> col 1
        ("subject-alpha", 600, 50, 8, 10),  # cols=6 -> off grid
        ("subject-alpha", -0.1, 50, 8, 10),  # below origin -> off grid
    )
    res = resolve_sightings(m, {"ev-geo-901": log})
    assert [(s.cell, s.location) for s in res.sightings] == [((1, 0), "LOC-GRID-01")]
    assert res.off_grid == 2
    assert res.unresolved == 0


def test_handle_location_and_unknown_subjects_dropped_and_counted():
    m = manifest_for(BASE)
    log = _log(
        "ev-geo-902",
        ("subject-alpha", 50, 50, 8, 10),  # kept (PERSON alias)
        ("vehicle-grey-van", 50, 50, 8, 10),  # kept (ASSET alias)
        ("kestrel-7@relay.example.invalid", 50, 50, 8, 10),  # HANDLE: dropped
        ("LOC-GRID-03", 50, 50, 8, 10),  # LOCATION: dropped
        ("subject-zulu", 50, 50, 8, 10),  # unknown: dropped
    )
    res = resolve_sightings(m, {"ev-geo-902": log})
    assert sorted(s.subject for s in res.sightings) == ["AST-VAN-1", "ENT-ALPHA"]
    assert res.unresolved == 3
    assert res.off_grid == 0
    s = res.sightings[0]
    assert s.evidence_id == "ev-geo-902"
    assert s.observation_id.startswith("ev-geo-902-o")


def test_lone_sighting_produces_no_candidate():
    _, got = _candidates(("subject-alpha", 50, 50, 8, 10))
    assert got == []


def test_candidates_sorted_by_location_then_start():
    _, got = _candidates(
        ("subject-alpha", 350, 250, 8, 10),
        ("subject-bravo", 350, 250, 9, 11),
        ("subject-charlie", 50, 50, 12, 14),
        ("subject-delta", 50, 50, 13, 15),
    )
    assert [c.location for c in got] == ["LOC-GRID-00", "LOC-GRID-15"]
