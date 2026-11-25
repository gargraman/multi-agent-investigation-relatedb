"""Per-agent oracle subset check (S2 spec §10.4, G8, G9; ADR-006 E5).

Used by the recording gate (``scripts/record_replay.py``, in-process ``FindingDraft`` output) and
by the AC17 E2E (``ContextFinding`` read back through the orchestrator proxy). Only the oracle's
``expected_findings`` whose ``agent`` matches are considered; other agents' expectations are
ignored until those agents exist.

Rules, each reported as a human-readable violation (empty list = pass):

1. The produced key set ``{(kind, assertion type, sorted(about))}`` **equals** the expected key set
   for the agent: a missing key and an extra key are both violations (exact equality, G8). Two
   produced findings with the same key are also a violation (an extra, by another name).
2. For every expected key, the produced finding's evidence ids are a superset of the expected ones.
3. No produced ``statement`` / ``reasoning_summary`` contains any ``forbidden_output_strings``
   (AC17 tripwire) or ``canary_strings`` (leak canary) entry. Matching is case-insensitive, which
   is stricter than the spec's "contains" and avoids trivially evaded hits.

A finding's assertion type is the single type of its assertions, or ``None`` when it has none. A
finding mixing assertion types matches no oracle entry (the oracle names one type per finding) and
is reported as such.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from nexus_contracts.common import AgentId
from nexus_contracts.scenario.oracle import Oracle

__all__ = [
    "EvidenceLike",
    "FindingKey",
    "FindingLike",
    "check_agent_subset",
    "find_forbidden",
    "finding_key",
]

FindingKey = tuple[str, str | None, tuple[str, ...]]
"""``(kind, assertion type or None, sorted about ids)``."""


@runtime_checkable
class EvidenceLike(Protocol):
    """``EvidenceRefSubmission`` (FindingDraft) and ``ContextEvidenceRef`` (ContextFinding)."""

    @property
    def evidence_id(self) -> str: ...


class _AssertionLike(Protocol):
    @property
    def type(self) -> str: ...


@runtime_checkable
class FindingLike(Protocol):
    """Satisfied by ``FindingDraft`` (api/findings.py) and ``ContextFinding`` (api/context.py).

    ``reasoning_summary`` is not part of the protocol because ``ContextFinding`` does not carry it;
    when present on the object it is checked too (read with ``getattr``).
    """

    @property
    def kind(self) -> str: ...

    @property
    def statement(self) -> str: ...

    @property
    def about(self) -> Sequence[str]: ...

    @property
    def evidence(self) -> Sequence[EvidenceLike]: ...

    @property
    def assertions(self) -> Sequence[_AssertionLike]: ...


def _str(value: object) -> str:
    # StrEnum members compare equal to their value, but str() of a plain Enum would not; use value.
    return str(getattr(value, "value", value))


def _assertion_types(finding: FindingLike) -> set[str]:
    return {_str(a.type) for a in finding.assertions}


def finding_key(finding: FindingLike) -> FindingKey:
    """The §10.4 identity of a finding. Mixed assertion types yield ``"MIXED:<a>+<b>"``."""
    types = sorted(_assertion_types(finding))
    if not types:
        assertion: str | None = None
    elif len(types) == 1:
        assertion = types[0]
    else:
        assertion = "MIXED:" + "+".join(types)
    return (_str(finding.kind), assertion, tuple(sorted(finding.about)))


def _fmt(key: FindingKey) -> str:
    kind, assertion, about = key
    return f"({kind}, {assertion}, [{', '.join(about)}])"


def find_forbidden(oracle: Oracle, texts: Iterable[tuple[str, str | None]]) -> list[str]:
    """Violations for every ``(label, text)`` containing a tripwire or canary (case-insensitive).

    Exposed so callers can also screen non-finding text (e.g. a TaskResult ``summary``)."""
    needles = [("forbidden output string", s) for s in oracle.forbidden_output_strings] + [
        ("canary string", s) for s in oracle.canary_strings
    ]
    out: list[str] = []
    for label, text in texts:
        if not text:
            continue
        folded = text.casefold()
        for what, needle in needles:
            if needle.casefold() in folded:
                out.append(f"{label} contains {what} {needle!r}")
    return out


def check_agent_subset(
    oracle: Oracle, agent_id: AgentId, findings: Sequence[FindingLike]
) -> list[str]:
    """Check ``findings`` produced by ``agent_id`` against that agent's oracle subset (§10.4)."""
    violations: list[str] = []
    agent = AgentId(agent_id)

    expected: dict[FindingKey, set[str]] = {}
    for f in oracle.expected_findings:
        if f.agent is not agent:
            continue
        key: FindingKey = (
            _str(f.kind),
            _str(f.assertion) if f.assertion is not None else None,
            tuple(sorted(f.about)),
        )
        expected.setdefault(key, set()).update(f.evidence)

    produced: dict[FindingKey, list[set[str]]] = {}
    for i, finding in enumerate(findings):
        key = finding_key(finding)
        if key[1] is not None and key[1].startswith("MIXED:"):
            violations.append(f"finding[{i}] {_fmt(key)} mixes assertion types")
        produced.setdefault(key, []).append({e.evidence_id for e in finding.evidence})

    # 1. exact key-set equality (no missing, no extras, no duplicates)
    for key in sorted(expected.keys() - produced.keys(), key=_fmt):
        violations.append(f"missing expected finding {_fmt(key)}")
    for key in sorted(produced.keys() - expected.keys(), key=_fmt):
        violations.append(f"unexpected extra finding {_fmt(key)}")
    for key in sorted(produced.keys(), key=_fmt):
        if len(produced[key]) > 1:
            violations.append(f"duplicate finding {_fmt(key)} produced {len(produced[key])} times")

    # 2. evidence superset, per expected key that was produced
    for key in sorted(expected.keys() & produced.keys(), key=_fmt):
        for got in produced[key]:
            missing = expected[key] - got
            if missing:
                violations.append(
                    f"finding {_fmt(key)} is missing expected evidence {sorted(missing)} "
                    f"(cites {sorted(got)})"
                )

    # 3. tripwires and canaries in free text
    texts: list[tuple[str, str | None]] = []
    for i, finding in enumerate(findings):
        label = f"finding[{i}] {_fmt(finding_key(finding))}"
        texts.append((f"{label} statement", finding.statement))
        texts.append((f"{label} reasoning_summary", getattr(finding, "reasoning_summary", None)))
    violations.extend(find_forbidden(oracle, texts))
    return violations
