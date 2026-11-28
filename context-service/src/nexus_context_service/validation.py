"""Graph-dependent validation of a findings batch (S2 spec FR-041, spec §9.3/§9.5, ADR-007).

The contract model already enforced the local rules (windows, ``subject in about``, canonical
``LINKED_TO`` order, no duplicates). This module checks what needs the graph or the caller:

- every ``about`` id is an entity of **this** investigation, of a type the caller may assert about
  (SIGINT: ``HANDLE``; GEOINT: ``PERSON | ASSET | LOCATION``, provisional until S3);
- every evidence ref exists in this investigation, its ``sha256`` matches the registered one
  (mismatch: ``evidence_integrity_error``), it is owned by the caller, ``ACTIVE``, and its
  classification is within the caller's clearance; the ref's ``owner_node_id`` names the caller;
- the finding's classification is ≥ the max of its evidence and ≤ the caller's clearance;
- assertions follow "who asserts what": SIGINT ``LINKED_TO`` between two ``HANDLE`` entities that
  are both in ``about``; GEOINT ``PLACED_AT`` a ``PERSON | ASSET`` at a ``LOCATION`` entity.

Pure: the repository passes in what it read inside the batch transaction. Error messages name
the rule only, never submitted values (spec §15.6); ``loc`` points at the offending field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nexus_contracts.api.findings import FindingBatchRequest, LinkedToAssertion
from nexus_contracts.api.problem import FieldError
from nexus_contracts.common import AgentId, Classification, EntityType

INTEGRITY_ERROR_TYPE = "evidence_integrity"


@dataclass(frozen=True)
class CallerRules:
    about_types: frozenset[str]
    assertion_types: frozenset[str]


RULES: dict[str, CallerRules] = {
    AgentId.SIGINT.value: CallerRules(
        about_types=frozenset({EntityType.HANDLE.value}),
        assertion_types=frozenset({"LINKED_TO"}),
    ),
    AgentId.GEOINT.value: CallerRules(
        about_types=frozenset(
            {EntityType.PERSON.value, EntityType.ASSET.value, EntityType.LOCATION.value}
        ),
        assertion_types=frozenset({"PLACED_AT"}),
    ),
}

_PLACEABLE = frozenset({EntityType.PERSON.value, EntityType.ASSET.value})


@dataclass(frozen=True)
class BatchValidation:
    errors: list[FieldError]
    integrity_error: bool  # at least one sha256 mismatch

    @property
    def ok(self) -> bool:
        return not self.errors


def referenced_ids(body: FindingBatchRequest) -> tuple[list[str], list[str]]:
    """Entity and evidence manifest ids the batch references (sorted, unique)."""
    entities: set[str] = set()
    evidence: set[str] = set()
    for f in body.findings:
        entities.update(f.about)
        evidence.update(e.evidence_id for e in f.evidence)
        for a in f.assertions:
            entities.add(a.subject)
            entities.add(a.object if isinstance(a, LinkedToAssertion) else a.location)
    return sorted(entities), sorted(evidence)


def _rank(value: str) -> int:
    return Classification(value).rank


def _fe(loc: list[str | int], msg: str, type_: str = "value_error") -> FieldError:
    return FieldError(loc=loc, msg=msg, type=type_)


def validate_batch(
    body: FindingBatchRequest,
    caller: str,
    entity_types: dict[str, str],
    evidence: dict[str, dict[str, Any]],
    clearance: Classification | None,
) -> BatchValidation:
    """``entity_types``: manifest_id -> type; ``evidence``: manifest_id -> stored row
    (``sha256``, ``owner``, ``classification``, ``status``), both for this investigation;
    ``clearance``: the caller's write clearance from the policy (``None``: may not submit)."""
    errors: list[FieldError] = []
    integrity = False
    rules = RULES.get(caller)
    if rules is None or clearance is None:  # policy allows only callers listed here
        return BatchValidation([_fe(["body"], "caller may not submit findings")], False)

    for i, f in enumerate(body.findings):
        base: list[str | int] = ["body", "findings", i]
        if f.classification.rank > clearance.rank:
            errors.append(_fe([*base, "classification"], "above the caller's clearance"))

        for j, mid in enumerate(f.about):
            etype = entity_types.get(mid)
            if etype is None:
                errors.append(_fe([*base, "about", j], "unknown entity in this investigation"))
            elif etype not in rules.about_types:
                errors.append(_fe([*base, "about", j], f"{etype} entities not allowed in about"))

        max_ev_rank = 0
        for k, ref in enumerate(f.evidence):
            loc: list[str | int] = [*base, "evidence", k]
            if ref.owner_node_id.value != caller:
                errors.append(_fe([*loc, "owner_node_id"], "must be the submitting agent"))
            stored = evidence.get(ref.evidence_id)
            if stored is None:
                errors.append(_fe([*loc, "evidence_id"], "unknown evidence in this investigation"))
                continue
            if stored["sha256"] != ref.sha256:
                integrity = True
                errors.append(
                    _fe(
                        [*loc, "sha256"],
                        "does not match the registered evidence hash",
                        INTEGRITY_ERROR_TYPE,
                    )
                )
            if stored["owner"] != caller:
                errors.append(_fe([*loc, "evidence_id"], "evidence is not owned by the caller"))
            if stored["status"] != "ACTIVE":
                errors.append(_fe([*loc, "evidence_id"], "evidence is not ACTIVE"))
            ev_rank = _rank(stored["classification"])
            max_ev_rank = max(max_ev_rank, ev_rank)
            if ev_rank > clearance.rank:
                errors.append(
                    _fe([*loc, "evidence_id"], "evidence classification above caller clearance")
                )
        if f.classification.rank < max_ev_rank:
            errors.append(
                _fe([*base, "classification"], "below the classification of its evidence")
            )

        about = set(f.about)
        for m, a in enumerate(f.assertions):
            aloc: list[str | int] = [*base, "assertions", m]
            if a.type not in rules.assertion_types:
                errors.append(_fe([*aloc, "type"], f"{a.type} not allowed for this caller"))
                continue
            if isinstance(a, LinkedToAssertion):
                for role, mid in (("subject", a.subject), ("object", a.object)):
                    if entity_types.get(mid) != EntityType.HANDLE.value:
                        errors.append(_fe([*aloc, role], "must be a HANDLE entity"))
                    if mid not in about:
                        errors.append(_fe([*aloc, role], "must be one of the finding's about"))
            else:
                if entity_types.get(a.subject) not in _PLACEABLE:
                    errors.append(_fe([*aloc, "subject"], "must be a PERSON or ASSET entity"))
                if entity_types.get(a.location) != EntityType.LOCATION.value:
                    errors.append(_fe([*aloc, "location"], "must be a LOCATION entity"))
    return BatchValidation(errors, integrity)
