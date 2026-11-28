"""GraphRepository: typed domain operations over the Relate lane (spec §4.3, ADR-001).

Each public method is **one** lane transaction, including its ``AuditEvent`` (spec §14.4).
Routes never see a connection or Cypher.

Audit policy (S1, extended in S2):
- a successful create / close writes one ``AuditEvent`` (``authz=ALLOWED, outcome=SUCCESS``);
- a committed findings batch writes one ``AuditEvent`` that ``CONCERNS`` its ``Submission``
  (S2 FR-043, G18);
- an idempotent **replay** (same create, close of a closed investigation, same findings batch)
  and a rejected conflict / validation failure (``409``/``422``) write **nothing**: no state
  changed, so there is nothing to audit, and a replay must leave the store byte-for-byte as it
  was (ADR-002 A1 "writing nothing new");
- a context read writes one ``AuditEvent`` in the same transaction as the read (S4a FR-204):
  ``outcome=FILTERED`` when clearance hid findings, else ``SUCCESS``; analysis reads also link it
  to the findings returned (``RETURNED``);
- a search writes the same read audit event (``operation=search``) in its own transaction;
- a committed assessment writes one ``AuditEvent`` that ``CONCERNS`` its ``Submission`` (S4a
  FR-209); a rejected citation rolls the write back and writes a standalone ``DENIED`` event
  (FR-211), with no finding id in it;
- a retraction writes one ``AuditEvent`` that ``CONCERNS`` its ``Decision`` (S4a FR-216); retracting
  already-retracted evidence returns the stored impact and writes nothing;
- an authorization denial writes a standalone ``AuditEvent`` (``authz=DENIED``; ADR-002).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import relate
from opentelemetry import trace

from nexus_context_service.assessments import (
    CitationRejected,
    FindingFacts,
    assessment_hash,
    assessment_key,
    used_finding_ids,
    validate_edge,
)
from nexus_context_service.ids import new_id
from nexus_context_service.lane import LaneUnavailable, RelateLane
from nexus_context_service.queries import assessments as aq_asm
from nexus_context_service.queries import audit as aq
from nexus_context_service.queries import context as cq
from nexus_context_service.queries import contradictions as xq
from nexus_context_service.queries import findings as fq
from nexus_context_service.queries import graph as gq
from nexus_context_service.queries import investigations as q
from nexus_context_service.queries import links as lq
from nexus_context_service.queries import provenance as pq
from nexus_context_service.queries import retraction as rq
from nexus_context_service.queries import search as sq
from nexus_context_service.queries.schema import SCHEMA_DDL
from nexus_context_service.validation import referenced_ids, validate_batch
from nexus_contracts.api.assessments import (
    Assessment,
    AssessmentSubmission,
    AssessmentSubmissionResponse,
    EdgeType,
    ProposedEdge,
    RejectedEdge,
)
from nexus_contracts.api.context import (
    ContextEvidenceRef,
    ContextFinding,
    ContextInvestigationClose,
    ContextInvestigationClosed,
    ContextInvestigationCreate,
    ContextInvestigationCreated,
    InvestigationContext,
    SearchRequest,
    SearchResponse,
)
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.findings import (
    FindingBatchRequest,
    LinkedToAssertion,
    PlacedAtAssertion,
)
from nexus_contracts.api.graph import (
    ContradictionPair,
    Graph,
    GraphEdge,
    GraphNode,
    GraphNodeLabel,
    HiddenLink,
    HiddenLinkWindow,
    Provenance,
    ProvenanceEvidence,
    ProvenanceFinding,
)
from nexus_contracts.api.problem import FieldError
from nexus_contracts.api.retraction import RetractionImpact
from nexus_contracts.api.states import InvestigationStatus
from nexus_contracts.common import Classification, EntityType, FindingKind, FindingStatus

if TYPE_CHECKING:  # scope.py imports this module (AuditContext); avoid the runtime cycle
    from nexus_context_service.scope import ReadScope

OPEN = "OPEN"
CLOSED = "CLOSED"

log = logging.getLogger(__name__)


class InvestigationNotFound(Exception):
    pass


class InvestigationContentConflict(Exception):
    pass


class InvestigationClosed(Exception):
    pass


class AlreadyCommitted(Exception):
    """A batch for this task was committed by the same caller with different content."""

    def __init__(self, finding_ids: list[str]) -> None:
        super().__init__("already committed")
        self.finding_ids = finding_ids


class AssessmentAlreadyCommitted(Exception):
    """An assessment for this task was committed with different content (FR-210)."""

    def __init__(self, assessment_id: str) -> None:
        super().__init__("already committed")
        self.assessment_id = assessment_id


class EvidenceNotFound(Exception):
    """No such evidence in this investigation (the id is investigation-scoped)."""


class AssessmentNotFound(Exception):
    """No such assessment, or one above the caller's clearance. The two are indistinguishable to
    the caller (404, §9.5); ``investigation_id`` is set only for the hidden case, so the route can
    audit the denial against the investigation without naming the hidden record."""

    def __init__(self, investigation_id: str | None = None) -> None:
        super().__init__("assessment not found")
        self.investigation_id = investigation_id

    @property
    def hidden(self) -> bool:
        return self.investigation_id is not None


class EntityNotFound(Exception):
    """No such entity in this investigation (or no such investigation)."""


class TaskOwnedByOtherCaller(Exception):
    """This task_id was committed by a different caller (S3 FR-130). Carries nothing: the other
    caller's finding ids and identity must not reach the response."""


class BatchInvalid(Exception):
    """FR-041 graph validation failed; nothing was written."""

    def __init__(self, errors: list[FieldError], integrity_error: bool) -> None:
        super().__init__("batch invalid")
        self.errors = errors
        self.integrity_error = integrity_error


@dataclass(frozen=True)
class BatchResult:
    finding_ids: list[str]
    created: bool  # False for an idempotent replay


def submission_key(task_id: str) -> str:
    """Idempotency key of a findings batch (S2 FR-040)."""
    return f"findings:{task_id}"


@dataclass(frozen=True)
class AuditContext:
    caller: str
    actor: str


def _now() -> datetime:
    return datetime.now(UTC)


def _trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def _write_audit(
    conn: relate.Connection,
    audit: AuditContext,
    *,
    operation: str,
    target_id: str | None,
    authz: str,
    outcome: str,
    now: datetime,
) -> None:
    conn.execute(
        q.CREATE_AUDIT_EVENT,
        {
            "audit_id": f"aud_{uuid.uuid4().hex}",
            "caller": audit.caller,
            "actor": audit.actor,
            "operation": operation,
            "target_id": target_id,
            "authz": authz,
            "outcome": outcome,
            "now": now,
            "trace_id": _trace_id(),
        },
    )


def _write_read_audit(
    conn: relate.Connection,
    scope: ReadScope,
    *,
    operation: str,
    investigation_id: str,
    filtered: int,
    returned_ids: list[str],
    now: datetime,
) -> None:
    """One ``AuditEvent`` for a read (S4a FR-204/FR-205): the **count** hidden by clearance,
    never their ids (NFR-202). When the scope carries a task id (analysis reads) the event also
    links ``RETURNED`` to each finding handed back: the retrieval receipt assessments cite."""
    audit_id = f"aud_{uuid.uuid4().hex}"
    conn.execute(
        aq.CREATE_READ_AUDIT_EVENT,
        {
            "audit_id": audit_id,
            "caller": scope.audit.caller,
            "actor": scope.audit.actor,
            "operation": operation,
            "target_id": investigation_id,
            "outcome": "FILTERED" if filtered else "SUCCESS",
            "now": now,
            "trace_id": _trace_id(),
            "filtered": filtered,
            "task_id": scope.task_id,
        },
    )
    if scope.task_id is not None and returned_ids:
        _count(
            conn,
            aq.CREATE_RETURNED,
            {"audit_id": audit_id, "finding_ids": returned_ids},
            len(returned_ids),
        )


def _count(conn: relate.Connection, cypher: str, params: dict[str, Any], expected: int) -> None:
    """Run a write that returns ``created``; a short count means an unmatched MATCH dropped rows,
    which must fail the transaction rather than commit a partial batch."""
    created = conn.execute(cypher, params).single()["created"]
    if created != expected:
        raise RuntimeError(f"batch write created {created} of {expected} rows")


def _batch_rows(
    inv: str, finding_ids: list[str], body: FindingBatchRequest
) -> dict[str, list[dict[str, Any]]]:
    """UNWIND parameter rows per statement (entity/evidence refs as investigation-scoped uids)."""
    out: dict[str, list[dict[str, Any]]] = {
        k: [] for k in ("findings", "about", "evidence", "linked_to", "placed_at")
    }
    for fid, f in zip(finding_ids, body.findings, strict=True):
        out["findings"].append(
            {
                "id": fid,
                "kind": f.kind.value,
                "statement": f.statement,
                "confidence": f.confidence,
                "confidence_band": f.confidence_band.value,
                "classification": f.classification.value,
                "classification_rank": f.classification.rank,
                "evidence_ids": [e.evidence_id for e in f.evidence],
                "model_id": f.model_id,
                "prompt_version": f.prompt_version,
                "schema_version": f.schema_version,
                "reasoning_summary": f.reasoning_summary,
                "valid_from": f.valid_from,
                "valid_until": f.valid_until,
                "synthetic": f.synthetic,
            }
        )
        out["about"] += [{"fid": fid, "uid": f"{inv}:{m}"} for m in f.about]
        out["evidence"] += [
            {"fid": fid, "uid": f"{inv}:{e.evidence_id}", "sha256": e.sha256, "summary": e.summary}
            for e in f.evidence
        ]
        for a in f.assertions:
            row = {
                "fid": fid,
                "aid": new_id("asr"),
                "s": f"{inv}:{a.subject}",
                "valid_from": a.valid_from,
                "valid_until": a.valid_until,
            }
            if isinstance(a, LinkedToAssertion):
                out["linked_to"].append(
                    {**row, "o": f"{inv}:{a.object}", "link_type": a.link_type.value}
                )
            else:
                out["placed_at"].append({**row, "l": f"{inv}:{a.location}"})
    return out


def _get(conn: relate.Connection, investigation_id: str) -> dict[str, Any] | None:
    rows = conn.execute(q.GET_INVESTIGATION, {"id": investigation_id}).fetchall()
    return dict(rows[0]) if rows else None


def _rows(conn: relate.Connection, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(cypher, params).fetchall()]


def _scalar(value: Any) -> Any:
    """A stored property as a graph ``props`` scalar: temporals become ISO-8601 strings."""
    return value.isoformat() if isinstance(value, datetime) else value


def _assemble_findings(
    conn: relate.Connection, params: dict[str, Any], findings: list[dict[str, Any]]
) -> list[ContextFinding]:
    """``ContextFinding`` rows for already-selected finding rows (kept in the given order): the
    one assembly shared by ``/context`` and ``/search`` (FR-044, FR-208). ``params`` carry the
    caller's read scope, so a hidden finding's edges are hidden with it."""
    if not findings:
        return []
    params = {**params, "finding_ids": [f["id"] for f in findings]}
    about: dict[str, list[str]] = {}
    for r in _rows(conn, cq.ABOUT, params):
        about.setdefault(r["fid"], []).append(r["manifest_id"])
    evidence: dict[str, list[ContextEvidenceRef]] = {}
    for r in _rows(conn, cq.EVIDENCE, params):
        evidence.setdefault(r.pop("fid"), []).append(ContextEvidenceRef(**r))
    assertions: dict[str, list[LinkedToAssertion | PlacedAtAssertion]] = {}
    for r in _rows(conn, cq.LINKED_TO, params):
        assertions.setdefault(r.pop("fid"), []).append(LinkedToAssertion(**r))
    for r in _rows(conn, cq.PLACED_AT, params):
        assertions.setdefault(r.pop("fid"), []).append(PlacedAtAssertion(**r))
    # Lists inside a finding carry no order semantics (content_hash sorts them too), so they
    # come back in a stable canonical order.
    return [
        ContextFinding(
            **f,
            about=sorted(about.get(f["id"], [])),
            evidence=sorted(evidence.get(f["id"], []), key=lambda e: e.evidence_id),
            assertions=sorted(assertions.get(f["id"], []), key=lambda a: a.sort_key()),
        )
        for f in findings
    ]


_BODY_JSON_FIELDS = ("conclusions", "contradictions", "information_gaps", "follow_up_questions")


def _apply_edges(
    conn: relate.Connection,
    edges: list[ProposedEdge],
    receipt: dict[str, dict[str, Any]],
    max_rank: int,
    assessment_id: str,
    now: str,
) -> tuple[list[ProposedEdge], list[RejectedEdge]]:
    """FR-213, in submission order (an edge sees the ones accepted before it, so a cycle can close
    within one submission). Endpoints are checked against the receipt and the caller's rank, the
    same test as the citation check. Called inside the assessment transaction."""
    visible = {
        fid: FindingFacts(kind=FindingKind(r["kind"]), status=FindingStatus(r["status"]))
        for fid, r in receipt.items()
        if r["classification_rank"] <= max_rank
    }
    accepted: list[ProposedEdge] = []
    rejected: list[RejectedEdge] = []
    for e in edges:
        params = {"src": e.source_finding_id, "tgt": e.target_finding_id}
        code = validate_edge(e, set(receipt), visible)
        if (
            code is None
            and e.type is EdgeType.DERIVED_FROM
            and conn.execute(aq_asm.DERIVED_FROM_CLOSES_CYCLE, params).single()["cycle"]
        ):
            code = ErrorCode.SCHEMA_INVALID
        if code is not None:
            rejected.append(RejectedEdge(edge=e, code=code))
            continue
        if not conn.execute(aq_asm.EDGE_PRESENT[e.type], params).single()["present"]:
            _count(
                conn,
                aq_asm.EDGE_CREATE[e.type],
                {**params, "assessment_id": assessment_id, "now": now},
                1,
            )
        if e.type is EdgeType.CONTRADICTS:
            conn.execute(aq_asm.DISPUTE_FINDINGS, {"ids": list(params.values())})
        accepted.append(e)
    return accepted, rejected


def _body_json(body: AssessmentSubmission) -> str:
    """Canonical JSON of an assessment's list fields, stored as one property (``body_json``)."""
    return json.dumps(
        body.model_dump(mode="json", include=set(_BODY_JSON_FIELDS)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _assessment(conn: relate.Connection, params: dict[str, Any]) -> Assessment | None:
    """The assessment ``/context`` shows (FR-207), read with the caller's scope params."""
    rows = _rows(
        conn, cq.ASSESSMENT, {**params, "assessment_statuses": list(cq.ASSESSMENT_STATUSES)}
    )
    if not rows:
        return None
    row = rows[0]
    return Assessment(**json.loads(row.pop("body_json")), **row)


class GraphRepository:
    def __init__(self, lane: RelateLane) -> None:
        self._lane = lane

    async def bootstrap_schema(self) -> None:
        def _ddl(conn: relate.Connection) -> None:
            for stmt in SCHEMA_DDL:
                conn.execute(stmt)

        # DDL runs in autocommit mode (as verified by the spike), still on the lane thread.
        await self._lane.run("schema.bootstrap", _ddl, transactional=False)

    async def create_investigation(
        self, body: ContextInvestigationCreate, audit: AuditContext
    ) -> ContextInvestigationCreated:
        content_hash = body.content_hash()
        inv = body.investigation_id

        def _tx(conn: relate.Connection) -> ContextInvestigationCreated:
            existing = _get(conn, inv)
            if existing is not None:
                if existing["write_state"] == CLOSED:
                    raise InvestigationClosed(inv)
                if existing["content_hash"] != content_hash:
                    raise InvestigationContentConflict(inv)
                return ContextInvestigationCreated(
                    investigation_id=inv,
                    created=False,
                    entity_count=existing["entity_count"],
                    evidence_count=existing["evidence_count"],
                )

            now = _now()
            conn.execute(
                q.CREATE_INVESTIGATION,
                {
                    "id": inv,
                    "content_hash": content_hash,
                    "scenario_id": body.scenario_id,
                    "scenario_version": body.scenario_version,
                    "objective": body.objective,
                    "grid_pitch": body.grid_pitch,
                    "now": now,
                    "status": InvestigationStatus.CREATED.value,
                },
            )
            plain, locations = [], []
            for e in body.entities:
                row = {
                    "manifest_id": e.manifest_id,
                    "type": e.type.value,
                    "display_name": e.display_name,
                    "aliases": list(e.aliases),
                }
                if e.type is EntityType.LOCATION:
                    row["x"] = e.position.x if e.position else None
                    row["y"] = e.position.y if e.position else None
                    locations.append(row)
                else:
                    plain.append(row)
            if plain:
                conn.execute(q.CREATE_ENTITIES, {"id": inv, "entities": plain})
            if locations:
                conn.execute(q.CREATE_LOCATIONS, {"id": inv, "locations": locations})
            if body.relationships:
                rels = [r.model_dump(mode="json") for r in body.relationships]
                created = conn.execute(
                    q.CREATE_RELATIONSHIPS, {"id": inv, "relationships": rels}
                ).single()["created"]
                if created != len(rels):  # guarded by request validation; never half-write
                    raise RuntimeError("relationship endpoint missing")
            conn.execute(
                q.CREATE_EVIDENCE,
                {"id": inv, "evidence": [ev.model_dump(mode="json") for ev in body.evidence]},
            )
            _write_audit(
                conn,
                audit,
                operation="investigations.create",
                target_id=inv,
                authz="ALLOWED",
                outcome="SUCCESS",
                now=now,
            )
            return ContextInvestigationCreated(
                investigation_id=inv,
                created=True,
                entity_count=len(body.entities),
                evidence_count=len(body.evidence),
            )

        return await self._lane.run("investigations.create", _tx)

    async def close_investigation(
        self, investigation_id: str, body: ContextInvestigationClose, audit: AuditContext
    ) -> ContextInvestigationClosed:
        def _tx(conn: relate.Connection) -> ContextInvestigationClosed:
            existing = _get(conn, investigation_id)
            if existing is None:
                raise InvestigationNotFound(investigation_id)
            if existing["write_state"] == CLOSED:  # idempotent: original status + closed_at
                return ContextInvestigationClosed(
                    investigation_id=investigation_id,
                    status=InvestigationStatus(existing["status"]),
                    closed_at=existing["closed_at"],
                )
            now = _now()
            conn.execute(
                q.CLOSE_INVESTIGATION,
                {"id": investigation_id, "status": body.status.value, "now": now},
            )
            _write_audit(
                conn,
                audit,
                operation="investigations.close",
                target_id=investigation_id,
                authz="ALLOWED",
                outcome="SUCCESS",
                now=now,
            )
            return ContextInvestigationClosed(
                investigation_id=investigation_id, status=body.status, closed_at=now
            )

        return await self._lane.run("investigations.close", _tx)

    async def submit_finding_batch(
        self, body: FindingBatchRequest, audit: AuditContext, clearance: Classification | None
    ) -> BatchResult:
        """One lane transaction: replay check -> fence -> FR-041 validation -> writes -> audit."""
        inv = body.investigation_id
        key = submission_key(body.task_id)
        content_hash = body.content_hash()
        caller = audit.caller

        def _tx(conn: relate.Connection) -> BatchResult:
            rows = conn.execute(fq.CHECK_SUBMISSION, {"inv": inv, "key": key}).fetchall()
            if not rows:
                raise InvestigationNotFound(inv)
            head = dict(rows[0])
            if head["finding_ids"] is not None:  # this task already committed (before fence, G15)
                if head["sub_caller"] != caller:  # caller-scoped idempotency, checked first
                    if head["write_state"] == CLOSED:  # the fence wins: no ownership hint
                        raise InvestigationClosed(inv)
                    raise TaskOwnedByOtherCaller()
                same = head["content_hash"] == content_hash and head["sub_inv"] == inv
                if same:
                    return BatchResult(finding_ids=list(head["finding_ids"]), created=False)
                raise AlreadyCommitted(list(head["finding_ids"]))
            if head["write_state"] == CLOSED:
                raise InvestigationClosed(inv)

            entity_ids, evidence_ids = referenced_ids(body)
            entity_types = {
                r["manifest_id"]: r["type"]
                for r in conn.execute(fq.GET_ENTITIES, {"inv": inv, "entity_ids": entity_ids})
            }
            evidence = {
                r["evidence_id"]: dict(r)
                for r in conn.execute(fq.GET_EVIDENCE, {"inv": inv, "evidence_ids": evidence_ids})
            }
            check = validate_batch(body, caller, entity_types, evidence, clearance)
            if not check.ok:
                raise BatchInvalid(check.errors, check.integrity_error)

            now = _now()
            submission_id = new_id("sub")
            finding_ids = [new_id("fnd") for _ in body.findings]
            common = {"inv": inv, "task_id": body.task_id, "attempt": body.attempt}
            conn.execute(
                fq.CREATE_SUBMISSION,
                {
                    **common,
                    "submission_id": submission_id,
                    "key": key,
                    "content_hash": content_hash,
                    "caller": caller,
                    "finding_ids": finding_ids,
                    "now": now,
                },
            )
            rows_by_kind = _batch_rows(inv, finding_ids, body)
            _count(
                conn,
                fq.CREATE_FINDINGS,
                {
                    **common,
                    "submission_id": submission_id,
                    "caller": caller,
                    "findings": rows_by_kind["findings"],
                    "now": now,
                },
                len(finding_ids),
            )
            for cypher, kind in (
                (fq.CREATE_ABOUT, "about"),
                (fq.CREATE_SUPPORTED_BY, "evidence"),
                (fq.CREATE_LINKED_TO, "linked_to"),
                (fq.CREATE_PLACED_AT, "placed_at"),
            ):
                if rows := rows_by_kind[kind]:
                    _count(conn, cypher, {"inv": inv, "rows": rows}, len(rows))
            _write_audit(
                conn,
                audit,
                operation="findings.submit",
                target_id=submission_id,
                authz="ALLOWED",
                outcome="SUCCESS",
                now=now,
            )
            return BatchResult(finding_ids=finding_ids, created=True)

        try:
            return await self._lane.run("findings.submit", _tx)
        except TaskOwnedByOtherCaller:
            # The batch transaction rolled back with the raise, so the denial is audited in its
            # own committed transaction. Target is the requested investigation, never the
            # owner's Submission: its ids must not appear in the audit row.
            try:
                await self.record_denial(audit, "findings.submit", inv)
            except LaneUnavailable:
                log.warning("denial not audited: store not ready", extra={"op": "findings.submit"})
            raise

    async def submit_assessment(
        self, body: AssessmentSubmission, audit: AuditContext, *, max_rank: int
    ) -> tuple[AssessmentSubmissionResponse, bool]:
        """FR-209..212 in one lane transaction, in this order (the order is the decision):

        1. investigation exists;
        2. idempotency: same key + hash replays the stored response, different hash is a 409
           (checked **before** the CLOSED fence, as the findings batch does (S2 G15), so a retry
           after close learns its assessment_id; and before citations, so a replay after a
           retraction still returns 200); then the investigation must be OPEN;
        3. the task's retrieval receipt within this investigation;
        4. every used id in the receipt and within ``max_rank`` (the caller's read clearance),
           else ``CitationRejected``: the transaction rolls back and a ``DENIED`` audit event
           (target = investigation, no finding id) is written in its own lane run;
        5. ``NEEDS_REVIEW`` if any used finding is no longer trusted, else ``ACTIVE`` (FR-211a);
        6. each proposed edge validated (FR-213) and, if accepted, written; each accepted
           ``CONTRADICTS`` moves its ACTIVE endpoints to DISPUTED (version + 1); a rejected edge
           never fails the assessment;
        7. Submission (holding the response, edges included), Assessment, BELONGS_TO,
           USES_FINDING, audit event CONCERNS Submission.

        Returns ``(response, created)``; ``created`` is False for a replay."""
        inv = body.investigation_id
        key = assessment_key(body.task_id)
        content_hash = assessment_hash(body)
        used = used_finding_ids(body)

        def _tx(conn: relate.Connection) -> tuple[AssessmentSubmissionResponse, bool]:
            rows = conn.execute(aq_asm.CHECK_SUBMISSION, {"inv": inv, "key": key}).fetchall()
            if not rows:
                raise InvestigationNotFound(inv)
            head = dict(rows[0])
            if head["response_json"] is not None:  # replay decided before the fence (G15)
                if head["content_hash"] == content_hash:
                    stored = AssessmentSubmissionResponse.model_validate_json(head["response_json"])
                    return stored, False
                raise AssessmentAlreadyCommitted(head["assessment_id"])
            if head["write_state"] == CLOSED:
                raise InvestigationClosed(inv)

            receipt = {
                r["id"]: r
                for r in _rows(
                    conn,
                    aq_asm.RECEIPT,
                    {"inv": inv, "caller": audit.caller, "task_id": body.task_id},
                )
            }
            cited = [receipt.get(fid) for fid in used]
            if any(r is None or r["classification_rank"] > max_rank for r in cited):
                raise CitationRejected()
            top = max(cited, key=lambda r: r["classification_rank"])
            trusted = all(r["status"] in cq.TRUSTED_STATUSES for r in cited)
            status = "ACTIVE" if trusted else "NEEDS_REVIEW"

            now = _now()
            submission_id = new_id("sub")
            assessment_id = new_id("asm")
            accepted, rejected = _apply_edges(
                conn, body.proposed_edges, receipt, max_rank, assessment_id, now
            )
            response = AssessmentSubmissionResponse(
                assessment_id=assessment_id, accepted_edges=accepted, rejected_edges=rejected
            )
            common = {
                "inv": inv,
                "task_id": body.task_id,
                "attempt": body.attempt,
                "submission_id": submission_id,
                "assessment_id": assessment_id,
                "now": now,
            }
            conn.execute(
                aq_asm.CREATE_SUBMISSION,
                {
                    **common,
                    "key": key,
                    "content_hash": content_hash,
                    "caller": audit.caller,
                    "response_json": response.model_dump_json(),
                },
            )
            _count(
                conn,
                aq_asm.CREATE_ASSESSMENT,
                {
                    **common,
                    "status": status,
                    "classification": top["classification"],
                    "classification_rank": top["classification_rank"],
                    "summary": body.summary,
                    "body_json": _body_json(body),
                    "model_id": body.model_id,
                    "prompt_version": body.prompt_version,
                    "schema_version": body.schema_version,
                },
                1,
            )
            _count(
                conn,
                aq_asm.CREATE_USES_FINDING,
                {"inv": inv, "assessment_id": assessment_id, "finding_ids": used},
                len(used),
            )
            _write_audit(
                conn,
                audit,
                operation="assessments.create",
                target_id=submission_id,
                authz="ALLOWED",
                outcome="SUCCESS",
                now=now,
            )
            return response, True

        try:
            return await self._lane.run("assessments.create", _tx)
        except CitationRejected:
            # The write rolled back with the raise; the denial is audited in its own committed
            # transaction (as for TaskOwnedByOtherCaller). Target is the investigation: no
            # finding id may appear in the audit row (FR-211, NFR-202).
            try:
                await self.record_denial(audit, "assessments.create", inv)
            except LaneUnavailable:
                log.warning(
                    "denial not audited: store not ready", extra={"op": "assessments.create"}
                )
            raise

    async def retract_evidence(
        self,
        investigation_id: str,
        evidence_id: str,
        reason: str | None,
        audit: AuditContext,
    ) -> tuple[RetractionImpact, bool]:
        """I2 / FR-216 in one lane transaction: evidence ``RETRACTED``, every dependent finding
        (``SUPPORTED_BY`` it, or reaching those through ``DERIVED_FROM`` at any depth) and every
        assessment using one ``NEEDS_REVIEW`` (``SUPERSEDED`` untouched and unlisted), a
        ``Decision`` that ``DISPOSES`` the evidence and stores the impact, and the audit event.
        Allowed on a CLOSED investigation (an operator disposition, not an agent write).

        Returns ``(impact, newly_retracted)``. Already ``RETRACTED``: the impact stored on the
        original ``Decision``, nothing written (no audit event either), ``False``."""
        uid = f"{investigation_id}:{evidence_id}"
        # ``audit.actor`` equals the caller when no valid X-Nexus-Actor was sent (or the caller
        # may not send one): that is "unknown operator", not the service identity.
        actor = "operator:unknown" if audit.actor == audit.caller else audit.actor

        def _tx(conn: relate.Connection) -> tuple[RetractionImpact, bool]:
            rows = conn.execute(rq.GET_EVIDENCE, {"inv": investigation_id, "uid": uid}).fetchall()
            if not rows:
                raise InvestigationNotFound(investigation_id)
            head = dict(rows[0])
            if head["status"] is None:
                raise EvidenceNotFound(uid)
            if head["status"] == "RETRACTED":
                if head["impact_json"] is None:  # RETRACTED is only ever set with a Decision
                    raise RuntimeError(f"evidence {uid} is RETRACTED without a Decision")
                return RetractionImpact.model_validate_json(head["impact_json"]), False

            row = conn.execute(rq.IMPACT, {"inv": investigation_id, "uid": uid}).single()
            decision_id = new_id("dec")
            impact = RetractionImpact(
                evidence_id=evidence_id,
                decision_id=decision_id,
                finding_ids=sorted(row["findings"]),
                assertion_ids=sorted(row["assertions"]),
                assessment_ids=sorted(row["assessments"]),
            )
            now = _now()
            conn.execute(rq.RETRACT_EVIDENCE, {"uid": uid, "now": now})
            conn.execute(rq.FLAG_FINDINGS, {"ids": impact.finding_ids})
            conn.execute(rq.FLAG_ASSESSMENTS, {"ids": impact.assessment_ids})
            _count(
                conn,
                rq.CREATE_DECISION,
                {
                    "uid": uid,
                    "inv": investigation_id,
                    "decision_id": decision_id,
                    "actor": actor,
                    "reason": reason,
                    "now": now,
                    "impact_json": impact.model_dump_json(),
                },
                1,
            )
            _write_audit(
                conn,
                audit,
                operation="evidence.retract",
                target_id=decision_id,
                authz="ALLOWED",
                outcome="SUCCESS",
                now=now,
            )
            return impact, True

        return await self._lane.run("evidence.retract", _tx)

    async def get_context(self, investigation_id: str, scope: ReadScope) -> InvestigationContext:
        """Read model (FR-044) filtered by the caller's scope (S4a FR-202). The read and its
        audit event are one lane transaction (NFR-203); an unknown investigation raises
        ``InvestigationNotFound`` and rolls both back."""
        params = {"inv": investigation_id, **scope.query_params()}

        def _tx(conn: relate.Connection) -> InvestigationContext:
            inv = _get(conn, investigation_id)
            if inv is None:
                raise InvestigationNotFound(investigation_id)
            findings = _rows(conn, cq.FINDINGS, params)
            filtered = conn.execute(cq.COUNT_FILTERED, params).single()["filtered"]
            _write_read_audit(
                conn,
                scope,
                operation="investigations.context",
                investigation_id=investigation_id,
                filtered=filtered,
                returned_ids=[f["id"] for f in findings],
                now=_now(),
            )
            return InvestigationContext(
                investigation_id=investigation_id,
                status=InvestigationStatus(inv["status"]),
                findings=_assemble_findings(conn, params, findings),
                assessment=_assessment(conn, params),
            )

        return await self._lane.run("investigations.context", _tx)

    async def search(self, body: SearchRequest, scope: ReadScope) -> SearchResponse:
        """Structured search (FR-208) filtered by the caller's scope. Same transaction shape as
        ``get_context``: read, hidden count and audit event are one lane transaction, and an
        unknown investigation raises ``InvestigationNotFound`` and rolls all of it back. The
        hidden count covers only findings matching the request's other filters."""
        inv_id = body.investigation_id
        params = {
            "inv": inv_id,
            **scope.query_params(),
            "entity_ids": body.entity_ids,
            "kinds": [k.value for k in body.kinds],
            "statuses": [s.value for s in body.statuses],
        }

        def _tx(conn: relate.Connection) -> SearchResponse:
            if _get(conn, inv_id) is None:
                raise InvestigationNotFound(inv_id)
            rows = _rows(conn, sq.SEARCH_FINDINGS, {**params, "fetch_limit": body.limit + 1})
            truncated = len(rows) > body.limit
            rows = rows[: body.limit]
            filtered = conn.execute(sq.COUNT_FILTERED_MATCHING, params).single()["filtered"]
            _write_read_audit(
                conn,
                scope,
                operation="search",
                investigation_id=inv_id,
                filtered=filtered,
                returned_ids=[f["id"] for f in rows],
                now=_now(),
            )
            return SearchResponse(
                findings=_assemble_findings(conn, params, rows), truncated=truncated
            )

        return await self._lane.run("search", _tx)

    async def graph(self, investigation_id: str, scope: ReadScope) -> Graph:
        """I6 / FR-219: the investigation's nodes and edges within the caller's scope. Findings
        and assessments above ``scope.max_rank`` are dropped with their assertions and edges
        and only *counted* (hidden findings plus hidden assessments) in the read's audit event
        (FR-204, NFR-202). The read and the audit event are one lane transaction; an unknown
        investigation raises ``InvestigationNotFound`` and rolls both back."""
        params = {"inv": investigation_id, **scope.query_params()}

        def _tx(conn: relate.Connection) -> Graph:
            if _get(conn, investigation_id) is None:
                raise InvestigationNotFound(investigation_id)
            nodes = [
                GraphNode(
                    id=row.pop("id"),
                    label=GraphNodeLabel(label),
                    props={k: _scalar(v) for k, v in row.items()},
                )
                for label, cypher in gq.NODE_QUERIES.items()
                for row in _rows(conn, cypher, params)
            ]
            edges = [
                GraphEdge(**row)
                for cypher in gq.EDGE_QUERIES
                for row in _rows(conn, cypher, params)
            ]
            filtered = (
                conn.execute(gq.COUNT_HIDDEN_FINDINGS, params).single()["filtered"]
                + conn.execute(gq.COUNT_HIDDEN_ASSESSMENTS, params).single()["filtered"]
            )
            _write_read_audit(
                conn,
                scope,
                operation="investigations.graph",
                investigation_id=investigation_id,
                filtered=filtered,
                returned_ids=[],  # receipts are for analysis reads of findings only
                now=_now(),
            )
            return Graph(nodes=nodes, edges=edges)

        return await self._lane.run("investigations.graph", _tx)

    async def provenance(self, assessment_id: str, max_rank: int) -> Provenance:
        """I1 / FR-215: the assessment's findings and their evidence, scoped to the assessment's
        own investigation and to ``max_rank``. Unknown, or the assessment's own
        ``classification_rank`` above ``max_rank``, raises ``AssessmentNotFound`` (the hidden case
        carries the investigation id for the route's ``DENIED`` audit). A trail, not a trusted
        read: retracted evidence and ``NEEDS_REVIEW`` findings are returned with their status.
        No read audit (not in FR-204's list)."""
        params = {"id": assessment_id, "max_rank": max_rank}

        def _tx(conn: relate.Connection) -> Provenance:
            rows = _rows(conn, pq.HEAD, params)
            if not rows:
                raise AssessmentNotFound()
            head = rows[0]
            if head["investigation_id"] is None:  # BELONGS_TO is written with every assessment
                raise RuntimeError(f"assessment {assessment_id} belongs to no investigation")
            if head["rank"] > max_rank:
                raise AssessmentNotFound(head["investigation_id"])
            findings: dict[str, ProvenanceFinding] = {}
            for r in _rows(conn, pq.TRAIL, {**params, "inv": head["investigation_id"]}):
                f = findings.setdefault(
                    r["finding_id"],
                    ProvenanceFinding(
                        finding_id=r["finding_id"],
                        source_agent_id=r["source_agent_id"],
                        status=FindingStatus(r["status"]),
                        classification=Classification(r["classification"]),
                        evidence=[],
                    ),
                )
                if r["evidence_id"] is not None:
                    f.evidence.append(
                        ProvenanceEvidence(
                            evidence_id=r["evidence_id"],
                            sha256=r["sha256"],
                            owner_node_id=r["owner_node_id"],
                            status=r["evidence_status"],
                        )
                    )
            return Provenance(assessment_id=assessment_id, findings=list(findings.values()))

        return await self._lane.run("assessments.provenance", _tx)

    async def contradictions(
        self, investigation_id: str, entity_id: str, max_rank: int
    ) -> list[ContradictionPair]:
        """I3 / FR-217: every ``CONTRADICTS`` pair between findings both ``ABOUT`` the entity,
        once, lower id first, both within ``max_rank``. Unknown entity (or investigation) raises
        ``EntityNotFound``. No read audit (not in FR-204's list)."""
        params = {
            "inv": investigation_id,
            "uid": f"{investigation_id}:{entity_id}",
            "max_rank": max_rank,
        }

        def _tx(conn: relate.Connection) -> list[ContradictionPair]:
            if conn.execute(xq.ENTITY_EXISTS, params).single()["n"] == 0:
                raise EntityNotFound(params["uid"])
            return [ContradictionPair(**r) for r in _rows(conn, xq.PAIRS, params)]

        return await self._lane.run("entities.contradictions", _tx)

    async def links(self, investigation_id: str, max_rank: int) -> list[HiddenLink]:
        """I4 / FR-218: the ADR-007 hidden-link query over this investigation, co-location radius
        1.5 x its ``grid_pitch``, every finding it reads within ``max_rank`` and trusted. A
        finding above ``max_rank`` is excluded from the whole computation (it appears nowhere and
        suppresses nothing). Unknown investigation raises ``InvestigationNotFound``. Sorted by
        ``(entity_a, via, entity_b)``; ids and windows sorted inside. No read audit (not in
        FR-204's list)."""

        def _tx(conn: relate.Connection) -> list[HiddenLink]:
            pitch = _rows(conn, lq.GRID_PITCH, {"inv": investigation_id})
            if not pitch:
                raise InvestigationNotFound(investigation_id)
            params = {
                "inv": investigation_id,
                "max_rank": max_rank,
                "radius": 1.5 * pitch[0]["pitch"],
            }
            out = [
                HiddenLink(
                    entity_a=r["entity_a"],
                    via=r["via"],
                    entity_b=r["entity_b"],
                    sigint_findings=sorted(r["sigint_findings"]),
                    geoint_findings=sorted(r["geoint_findings"]),
                    windows=sorted(
                        (HiddenLinkWindow.model_validate(w) for w in r["windows"]),
                        key=lambda w: (w.from_, w.until, w.cell_c, w.cell_b),
                    ),
                    disputed=r["disputed"],
                )
                for r in _rows(conn, lq.HIDDEN_LINKS, params)
            ]
            return sorted(out, key=lambda x: (x.entity_a, x.via, x.entity_b))

        return await self._lane.run("investigations.links", _tx)

    async def record_denial(
        self, audit: AuditContext, operation: str, target_id: str | None
    ) -> None:
        def _tx(conn: relate.Connection) -> None:
            _write_audit(
                conn,
                audit,
                operation=operation,
                target_id=target_id,
                authz="DENIED",
                outcome="DENIED",
                now=_now(),
            )

        await self._lane.run("audit.denial", _tx)
