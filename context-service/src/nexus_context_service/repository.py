"""GraphRepository: typed domain operations over the Relate lane (spec §4.3, ADR-001).

Each public method is **one** lane transaction, including its ``AuditEvent`` (spec §14.4).
Routes never see a connection or Cypher.

Audit policy for S1:
- a successful create / close writes one ``AuditEvent`` (``authz=ALLOWED, outcome=SUCCESS``);
- an idempotent **replay** (same create, or close of a closed investigation) and a rejected
  conflict (``409``) write **nothing**: no state changed, so there is nothing to audit, and a
  replay must leave the store byte-for-byte as it was (ADR-002 A1 "writing nothing new");
- an authorization denial writes a standalone ``AuditEvent`` (``authz=DENIED``; ADR-002).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import relate
from opentelemetry import trace

from nexus_context_service.lane import RelateLane
from nexus_context_service.queries import investigations as q
from nexus_context_service.queries.schema import SCHEMA_DDL
from nexus_contracts.api.context import (
    ContextInvestigationClose,
    ContextInvestigationClosed,
    ContextInvestigationCreate,
    ContextInvestigationCreated,
)
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
