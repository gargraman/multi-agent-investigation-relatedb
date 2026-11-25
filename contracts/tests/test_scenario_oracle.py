"""Independent oracle verifier for the scenario pack (no graph DB, no nexus models).

Re-derives from the raw fixtures (json + yaml only, its own grid maths) everything the oracle
claims, and checks that it is the WHOLE truth: the set of I4 hidden paths, contradictions and
corroborations computed over signal AND noise must equal the oracle exactly. Mutation tests
prove the verifier notices noise that adds a direct A-B link or an extra path.

Semantics mirror ADR-007: handles resolve to people only via manifest ``used_by``; co-location =
cell centres within 1.5 x pitch; half-open windows overlap iff a.from < b.until, b.from < a.until.
Working on raw records (not detector-aggregated findings) is conservative: any contact or
co-location a detector could ever report is considered.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data"
SCENARIOS = ["baseline-corroboration", "contradiction", "prompt-injection"]


def _ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


@dataclass
class Dataset:
    manifest: dict
    # (evidence_id, from_mention, to_mention, start, end)
    contacts: list[tuple[str, str, str, datetime, datetime]] = field(default_factory=list)
    # (evidence_id, subject_mention, x, y, from, until)
    sightings: list[tuple[str, str, float, float, datetime, datetime]] = field(default_factory=list)


def load_dataset(scenario: str) -> Dataset:
    manifest = yaml.safe_load((DATA / "scenarios" / scenario / "manifest.yaml").read_text())
    ds = Dataset(manifest=manifest)
    for ev in manifest["evidence"]:
        doc = json.loads((DATA / "fixtures" / ev["file"]).read_text())
        assert doc["evidence_id"] == ev["id"]
        if ev["owner_node"] == "sigint-agent":
            for r in doc["records"]:
                start = _ts(r["start"])
                end = start + timedelta(seconds=r["duration_s"])
                ds.contacts.append((ev["id"], r["from_handle"], r["to_handle"], start, end))
        else:
            for o in doc["observations"]:
                ds.sightings.append(
                    (
                        ev["id"],
                        o["subject"],
                        o["x"],
                        o["y"],
                        _ts(o["observed_from"]),
                        _ts(o["observed_until"]),
                    )
                )
    return ds


class World:
    """Facts derived from a Dataset."""

    def __init__(self, ds: Dataset) -> None:
        m = ds.manifest
        g = m["grid"]
        self.pitch = float(g["pitch"])
        self.ox, self.oy = float(g["origin"]["x"]), float(g["origin"]["y"])
        self.cols, self.rows = int(g["cols"]), int(g["rows"])
        self.types = {e["id"]: e["type"] for e in m["entities"]}
        self.names: dict[str, str] = {}
        for e in m["entities"]:
            for n in [e["id"], *e.get("aliases", [])]:
                self.names[n] = e["id"]
        self.person_of = {e["id"]: e["used_by"] for e in m["entities"] if e.get("used_by")}

        # person-level contacts: frozenset{p, q} -> [(evidence, start, end)]
        self.contacts: dict[frozenset[str], list[tuple[str, datetime, datetime]]] = {}
        for ev, f, t, start, end in ds.contacts:
            hf, ht = self.names.get(f), self.names.get(t)
            p, q = self.person_of.get(hf or ""), self.person_of.get(ht or "")
            if p and q and p != q:
                self.contacts.setdefault(frozenset((p, q)), []).append((ev, start, end))

        # sightings snapped to cell centres; unresolved subjects keep their mention as identity
        self.sightings = []
        for ev, subj, x, y, a, b in ds.sightings:
            col = math.floor((x - self.ox) / self.pitch)
            row = math.floor((y - self.oy) / self.pitch)
            assert 0 <= col < self.cols and 0 <= row < self.rows, f"{ev}: point off grid"
            centre = (self.ox + (col + 0.5) * self.pitch, self.oy + (row + 0.5) * self.pitch)
            self.sightings.append((ev, self.names.get(subj, f"U:{subj}"), centre, (a, b)))

    def colocations(self) -> dict[frozenset[str], list[tuple[str, str, tuple]]]:
        """frozenset{s, t} -> [(ev_s, ev_t, overlap window)] for distinct subjects."""
        out: dict[frozenset[str], list] = {}
        radius = 1.5 * self.pitch
        for i, (e1, s1, c1, w1) in enumerate(self.sightings):
            for e2, s2, c2, w2 in self.sightings[i + 1 :]:
                if s1 != s2 and math.dist(c1, c2) <= radius + 1e-9 and _overlap(w1, w2):
                    win = (max(w1[0], w2[0]), min(w1[1], w2[1]))
                    out.setdefault(frozenset((s1, s2)), []).append((e1, e2, win))
        return out

    def manifest_related(self, a: str, b: str) -> bool:
        return self.person_of.get(a) == b or self.person_of.get(b) == a

    def direct_link(self, a: str, b: str, coloc: dict) -> bool:
        pair = frozenset((a, b))
        return self.manifest_related(a, b) or pair in self.contacts or pair in coloc

    def hidden_paths(self) -> set[tuple[str, str, str]]:
        coloc = self.colocations()
        paths = set()
        for pair in self.contacts:
            for a, c in (tuple(pair), tuple(pair)[::-1]):
                for cpair in coloc:
                    if c not in cpair:
                        continue
                    (b,) = cpair - {c}
                    if b in (a, c) or self.direct_link(a, b, coloc):
                        continue
                    paths.add((a, c, b))
        return paths

    def contradictions(self) -> set[tuple[str, frozenset[str]]]:
        out = set()
        radius = 1.5 * self.pitch
        for i, (e1, s1, c1, w1) in enumerate(self.sightings):
            for e2, s2, c2, w2 in self.sightings[i + 1 :]:
                if s1 == s2 and _overlap(w1, w2) and math.dist(c1, c2) > radius + 1e-9:
                    out.add((s1, frozenset((e1, e2))))
        return out

    def corroborations(self) -> set[tuple[frozenset[str], frozenset[str]]]:
        """Person pairs whose contact falls in (overlaps) a co-location window of the same pair."""
        out = set()
        coloc = self.colocations()
        for pair, calls in self.contacts.items():
            for e_s, cs, ce in calls:
                for e1, e2, win in coloc.get(pair, []):
                    if _overlap((cs, ce), win):
                        out.add((pair, frozenset((e_s, e1)) | frozenset((e_s, e2))))
        return out


def _oracle_paths(m: dict) -> set[tuple[str, str, str]]:
    return {(p["a"], p["via"], p["b"]) for p in m["oracle"]["hidden_paths"]}


def _oracle_contradictions(m: dict) -> set[tuple[str, frozenset[str]]]:
    return {(c["about"][0], frozenset(c["evidence"])) for c in m["oracle"]["contradictions"]}


def _oracle_corroborations(m: dict) -> set[tuple[frozenset[str], frozenset[str]]]:
    return {
        (frozenset(c["about"]), frozenset(c["evidence"])) for c in m["oracle"]["corroborations"]
    }


# -- whole-truth checks for every scenario ---------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_hidden_paths_equal_oracle(scenario: str) -> None:
    ds = load_dataset(scenario)
    assert World(ds).hidden_paths() == _oracle_paths(ds.manifest)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_contradictions_equal_oracle(scenario: str) -> None:
    ds = load_dataset(scenario)
    assert World(ds).contradictions() == _oracle_contradictions(ds.manifest)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_corroborations_equal_oracle(scenario: str) -> None:
    ds = load_dataset(scenario)
    assert World(ds).corroborations() == _oracle_corroborations(ds.manifest)


# -- baseline: the planted ADR-007 I4 pattern, checked piece by piece --------------------------


def test_baseline_plants_full_i4_pattern() -> None:
    ds = load_dataset("baseline-corroboration")
    w = World(ds)
    (path,) = ds.manifest["oracle"]["hidden_paths"]
    a, c, b = path["a"], path["via"], path["b"]
    assert w.types[a] == w.types[c] == "PERSON"
    assert w.types[b] in ("PERSON", "ASSET")

    # hA USED_BY A, hC USED_BY C; SIGINT contact hA<->hC with timestamps, from SIGINT evidence
    contacts = w.contacts.get(frozenset((a, c)), [])
    assert len(contacts) >= 2, "recurring A-C handle contact expected"
    owners = {e["id"]: e["owner_node"] for e in ds.manifest["evidence"]}
    assert {owners[ev] for ev, _, _ in contacts} == {"sigint-agent"}

    # GEOINT: C and B in neighbouring cells (<= 1.5 x pitch) with overlapping windows
    coloc = w.colocations().get(frozenset((c, b)), [])
    assert coloc, "C and B must be co-located"
    assert all(owners[e1] == owners[e2] == "geoint-agent" for e1, e2, _ in coloc)

    # No direct A-B link of any kind (G4), across signal AND noise
    assert not w.manifest_related(a, b)
    handles_a = {h for h, p in w.person_of.items() if p == a}
    handles_b = {h for h, p in w.person_of.items() if p == b}
    assert handles_a and handles_b
    for _, f, t, _, _ in ds.contacts:
        pair = {w.names.get(f), w.names.get(t)}
        assert not (pair & handles_a and pair & handles_b), f"direct A-B contact: {f} <-> {t}"
    assert frozenset((a, b)) not in w.colocations(), "A and B must never be co-located"


def test_contradiction_scenario_conflict_is_real() -> None:
    ds = load_dataset("contradiction")
    w = World(ds)
    (conflict,) = ds.manifest["oracle"]["contradictions"]
    subject = conflict["about"][0]
    hits = [s for s in w.sightings if s[1] == subject and s[0] in set(conflict["evidence"])]
    assert len(hits) == 2
    (e1, _, c1, w1), (e2, _, c2, w2) = hits
    assert e1 != e2 and _overlap(w1, w2)
    assert math.dist(c1, c2) > 1.5 * w.pitch


# -- mutation tests: the verifier must catch noise that breaks the oracle ----------------------


def _baseline() -> Dataset:
    return copy.deepcopy(load_dataset("baseline-corroboration"))


T = _ts("2026-10-14T10:15:00Z")


def test_detects_injected_direct_contact_between_a_and_b() -> None:
    ds = _baseline()
    # A's SECOND handle talks to B's handle: must kill the hidden path
    ds.contacts.append(
        (
            "ev-sig-001",
            "kestrel-alt@mesh.example.invalid",
            "osprey-9@relay.example.invalid",
            T,
            T + timedelta(seconds=60),
        )
    )
    assert World(ds).hidden_paths() != _oracle_paths(ds.manifest)


def test_detects_injected_colocation_of_a_and_b() -> None:
    ds = _baseline()
    ds.sightings.append(("ev-geo-001", "subject-alpha", 455.0, 250.0, T, T + timedelta(hours=1)))
    assert ("ENT-ALPHA", "ENT-CHARLIE", "ENT-BRAVO") not in World(ds).hidden_paths()


def test_detects_noise_creating_an_extra_hidden_path() -> None:
    ds = _baseline()
    # an asset parked next to CHARLIE during the window -> extra path A -> C -> asset
    ds.sightings.append(("ev-geo-001", "vehicle-grey-van", 150.0, 250.0, T, T + timedelta(hours=1)))
    assert ("ENT-ALPHA", "ENT-CHARLIE", "AST-VAN-1") in World(ds).hidden_paths()
    assert World(ds).hidden_paths() != _oracle_paths(ds.manifest)


def test_detects_noise_creating_an_extra_contradiction() -> None:
    ds = _baseline()
    ds.sightings.append(("ev-geo-001", "subject-charlie", 550.0, 50.0, T, T + timedelta(hours=1)))
    assert World(ds).contradictions() != _oracle_contradictions(ds.manifest)


def test_detects_broken_planted_window() -> None:
    ds = _baseline()
    # move BRAVO's sighting to the next day: no overlap -> planted path disappears
    ds.sightings = [
        (e, s, x, y, a + timedelta(days=1), b + timedelta(days=1))
        if s == "subject-bravo" and e == "ev-geo-007"
        else (e, s, x, y, a, b)
        for e, s, x, y, a, b in ds.sightings
    ]
    assert World(ds).hidden_paths() != _oracle_paths(ds.manifest)


def test_diagonal_neighbour_counts_two_cells_apart_does_not() -> None:
    ds = _baseline()
    far = copy.deepcopy(ds)
    # CHARLIE's cell is (2,2) centre (250,250); two cells away on x is not a neighbour
    far.sightings.append(("ev-geo-001", "subject-foxtrot", 50.0, 250.0, T, T + timedelta(hours=1)))
    assert frozenset(("ENT-CHARLIE", "ENT-FOXTROT")) not in World(far).colocations()
    diag = copy.deepcopy(ds)
    diag.sightings.append(
        ("ev-geo-001", "subject-foxtrot", 150.0, 150.0, T, T + timedelta(hours=1))
    )
    assert frozenset(("ENT-CHARLIE", "ENT-FOXTROT")) in World(diag).colocations()
