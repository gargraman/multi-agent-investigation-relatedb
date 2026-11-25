from __future__ import annotations

from datetime import timedelta

from nexus_contracts.scenario.fixtures import CommsLog
from nexus_contracts.scenario.loader import load_evidence
from nexus_sigint.detectors import recurring_contacts

from .conftest import FIXTURES, ScenarioCopy, manifest_for


def _evidence(manifest, fixtures=FIXTURES) -> dict[str, CommsLog]:
    return {
        ref.id: load_evidence(ref, fixtures)
        for ref in manifest.evidence
        if ref.owner_node.value == "sigint-agent"
    }


def test_baseline_pairs_match_oracle() -> None:
    m = manifest_for("baseline-corroboration")
    cands = recurring_contacts(m, _evidence(m))
    assert [(c.subject, c.object) for c in cands] == [
        ("HDL-ALPHA-1", "HDL-CHARLIE-1"),
        ("HDL-DELTA-1", "HDL-ECHO-1"),
    ]
    expected = {
        tuple(sorted(f.about)): f.evidence
        for f in m.oracle.expected_findings
        if f.agent.value == "sigint-agent"
    }
    for c in cands:
        assert set(expected[(c.subject, c.object)]) <= set(c.evidence_ids)
    assert [len(c.contacts) for c in cands] == [4, 2]


def test_contradiction_pair() -> None:
    m = manifest_for("contradiction")
    cands = recurring_contacts(m, _evidence(m))
    assert [(c.subject, c.object, c.evidence_ids) for c in cands] == [
        ("HDL-ALPHA-1", "HDL-BRAVO-1", ["ev-sig-006"])
    ]


def test_windows_are_half_open_and_ordered() -> None:
    m = manifest_for("baseline-corroboration")
    cand = recurring_contacts(m, _evidence(m))[0]
    starts = [c.start for c in cand.contacts]
    assert starts == sorted(starts)
    for c in cand.contacts:
        assert c.end == c.start + timedelta(seconds=c.duration_s)
    assert cand.valid_from == starts[0]
    assert cand.valid_until == max(c.end for c in cand.contacts)


def test_threshold_and_canonical_order(baseline_copy: ScenarioCopy) -> None:
    # Reverse direction on every record of ev-sig-004 and drop all but one record of ev-sig-009.
    def flip(data: dict) -> None:
        for r in data["records"]:
            r["from_handle"], r["to_handle"] = r["to_handle"], r["from_handle"]

    baseline_copy.edit_fixture("ev-sig-004", flip, rehash=True)
    baseline_copy.edit_fixture(
        "ev-sig-009", lambda d: d.update(records=d["records"][:1]), rehash=True
    )
    m = baseline_copy.manifest
    cands = recurring_contacts(m, _evidence(m, baseline_copy.fixtures))
    assert [(c.subject, c.object) for c in cands] == [("HDL-ALPHA-1", "HDL-CHARLIE-1")]


def test_unresolved_and_non_handle_mentions_ignored(baseline_copy: ScenarioCopy) -> None:
    # Person aliases resolve to PERSON entities, not HANDLEs -> ignored; unknown strings ignored.
    def plant(data: dict) -> None:
        base = data["records"][0]
        a, b = "subject-alpha", "subject-bravo"
        data["records"] += [
            base | {"record_id": "x1", "from_handle": a, "to_handle": b},
            base | {"record_id": "x2", "from_handle": a, "to_handle": b},
            base | {"record_id": "x3", "from_handle": "nobody@x.invalid", "to_handle": "osprey-9"},
            base | {"record_id": "x4", "from_handle": "nobody@x.invalid", "to_handle": "osprey-9"},
        ]

    baseline_copy.edit_fixture("ev-sig-001", plant, rehash=True)
    m = baseline_copy.manifest
    cands = recurring_contacts(m, _evidence(m, baseline_copy.fixtures))
    assert [(c.subject, c.object) for c in cands] == [
        ("HDL-ALPHA-1", "HDL-CHARLIE-1"),
        ("HDL-DELTA-1", "HDL-ECHO-1"),
    ]


def test_records_across_evidence_count_together(baseline_copy: ScenarioCopy) -> None:
    # One ALPHA-1 <-> BRAVO-1 record in each of two evidence items -> a candidate spanning both.
    def add(handle_from: str, handle_to: str, rid: str):
        def fn(data: dict) -> None:
            r = data["records"][0] | {
                "record_id": rid,
                "from_handle": handle_from,
                "to_handle": handle_to,
            }
            data["records"].append(r)

        return fn

    baseline_copy.edit_fixture(
        "ev-sig-001", add("kestrel-7@relay.example.invalid", "HDL-BRAVO-1", "y1"), rehash=True
    )
    baseline_copy.edit_fixture(
        "ev-sig-002", add("osprey-9@relay.example.invalid", "HDL-ALPHA-1", "y2"), rehash=True
    )
    m = baseline_copy.manifest
    cands = recurring_contacts(m, _evidence(m, baseline_copy.fixtures))
    ab = [c for c in cands if (c.subject, c.object) == ("HDL-ALPHA-1", "HDL-BRAVO-1")]
    assert len(ab) == 1 and ab[0].evidence_ids == ["ev-sig-001", "ev-sig-002"]
