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
- an authorization denial writes a standalone ``AuditEvent`` (``authz=DENIED``; ADR-002).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import relate
from opentelemetry import trace

from nexus_context_service.ids import new_id
from nexus_context_service.lane import RelateLane
from nexus_context_service.queries import context as cq
from nexus_context_service.queries import findings as fq
from nexus_context_service.queries import investigations as q
from nexus_context_service.queries.schema import SCHEMA_DDL
from nexus_context_service.validation import referenced_ids, validate_batch
from nexus_contracts.api.context import (
    ContextEvidenceRef,
    ContextFinding,
    ContextInvestigationClose,
    ContextInvestigationClosed,
    ContextInvestigationCreate,
    ContextInvestigationCreated,
    InvestigationContext,
)
from nexus_contracts.api.findings import (
    FindingBatchRequest,
    LinkedToAssertion,
    PlacedAtAssertion,
)
from nexus_contracts.api.problem import FieldError
from nexus_contracts.api.states import InvestigationStatus
from nexus_contracts.common import EntityType

OPEN = "OPEN"
CLOSED = "CLOSED"


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
        self, body: FindingBatchRequest, audit: AuditContext
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
            check = validate_batch(body, caller, entity_types, evidence)
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

        return await self._lane.run("findings.submit", _tx)

    async def get_context(self, investigation_id: str) -> InvestigationContext:
        """Read model (FR-044). Orchestrator view: DEMO-HIGH clearance, so nothing is filtered."""
        params = {"inv": investigation_id}

        def _rows(conn: relate.Connection, cypher: str) -> list[dict[str, Any]]:
            return [dict(r) for r in conn.execute(cypher, params).fetchall()]

        def _tx(conn: relate.Connection) -> InvestigationContext:
            inv = _get(conn, investigation_id)
            if inv is None:
                raise InvestigationNotFound(investigation_id)
            findings = _rows(conn, cq.FINDINGS)
            about: dict[str, list[str]] = {}
            for r in _rows(conn, cq.ABOUT):
                about.setdefault(r["fid"], []).append(r["manifest_id"])
            evidence: dict[str, list[ContextEvidenceRef]] = {}
            for r in _rows(conn, cq.EVIDENCE):
                evidence.setdefault(r.pop("fid"), []).append(ContextEvidenceRef(**r))
            assertions: dict[str, list[LinkedToAssertion | PlacedAtAssertion]] = {}
            for r in _rows(conn, cq.LINKED_TO):
                assertions.setdefault(r.pop("fid"), []).append(LinkedToAssertion(**r))
            for r in _rows(conn, cq.PLACED_AT):
                assertions.setdefault(r.pop("fid"), []).append(PlacedAtAssertion(**r))
            # Lists inside a finding carry no order semantics (content_hash sorts them too), so
            # they come back in a stable canonical order.
            return InvestigationContext(
                investigation_id=investigation_id,
                status=InvestigationStatus(inv["status"]),
                findings=[
                    ContextFinding(
                        **f,
                        about=sorted(about.get(f["id"], [])),
                        evidence=sorted(evidence.get(f["id"], []), key=lambda e: e.evidence_id),
                        assertions=sorted(assertions.get(f["id"], []), key=lambda a: a.sort_key()),
                    )
                    for f in findings
                ],
            )

        return await self._lane.run("investigations.context", _tx)

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
