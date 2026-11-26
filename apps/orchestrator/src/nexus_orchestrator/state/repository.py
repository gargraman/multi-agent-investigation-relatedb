"""Repository over the §8.7 tables. Reads use the read connection; writes take a ``Transaction``.

Every row is mapped to its contract model on the way out, so a bad row fails loudly here rather
than leaking to the API.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import aiosqlite

from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.investigations import Investigation, TaskView
from nexus_contracts.api.states import InvestigationStatus, OrchestratorTaskState
from nexus_contracts.common import AgentId
from nexus_contracts.events.timeline import (
    InvestigationStatusChanged,
    TaskStatusChanged,
    TimelineEvent,
    TimelineEventType,
)
from nexus_orchestrator.ids import iso, parse_iso
from nexus_orchestrator.pubsub import TimelineBroker
from nexus_orchestrator.state.db import Database, Transaction
from nexus_orchestrator.workflow.transitions import (
    MAX_ATTEMPTS,
    IllegalTransition,
    check_task_insert,
    investigation_allowed,
    task_allowed,
)

log = logging.getLogger(__name__)

ACTIVE_STATUSES = (
    InvestigationStatus.CREATED,
    InvestigationStatus.RUNNING,
    InvestigationStatus.ANALYZING,
)


class InvalidCursor(ValueError):
    pass


class Applied(StrEnum):
    """Outcome of a guarded status write (ADR-008 F4)."""

    APPLIED = "applied"
    NOOP = "noop"  # already in the target state: nothing written, no event
    DROPPED = "dropped"  # edge not allowed from the current state: transition.dropped written


def _usage_tokens(usage_json: str | None) -> int | None:
    """``input_tokens + output_tokens`` from a ``usage_json`` blob; None if absent or malformed."""
    if not usage_json:
        return None
    try:
        usage = json.loads(usage_json)
        if not isinstance(usage, dict):
            return None
        return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class TaskRecord:
    """Internal view of a ``tasks`` row, including the ``version`` used for compare-and-set."""

    id: str
    investigation_id: str
    agent_id: AgentId
    task_type: str
    status: OrchestratorTaskState
    status_reason: str | None
    attempt: int
    deadline: datetime
    token_reservation: int
    acked_at: str | None
    version: int
    updated_at: datetime
    usage_tokens: int | None = None  # input + output tokens from ``usage_json``; None if unset

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> TaskRecord:
        return cls(
            id=row["id"],
            investigation_id=row["investigation_id"],
            agent_id=AgentId(row["agent_id"]),
            task_type=row["task_type"],
            status=OrchestratorTaskState(row["status"]),
            status_reason=row["status_reason"],
            attempt=row["attempt"],
            deadline=parse_iso(row["deadline"]),
            token_reservation=row["token_reservation"],
            acked_at=row["acked_at"],
            version=row["version"],
            updated_at=parse_iso(row["updated_at"]),
            usage_tokens=_usage_tokens(row["usage_json"]),
        )


@dataclass(frozen=True)
class InvestigationRecord:
    id: str
    scenario_id: str
    objective: str
    status: InvestigationStatus
    deadline: datetime
    trace_context_json: str | None
    version: int
    context_closed: bool = False  # the Context Service accepted the close (FR-125, FR-123)
    tokens_used: int = 0  # cumulative input + output over every terminal task write (all attempts)

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> InvestigationRecord:
        return cls(
            id=row["id"],
            scenario_id=row["scenario_id"],
            objective=row["objective"],
            status=InvestigationStatus(row["status"]),
            deadline=parse_iso(row["deadline"]),
            trace_context_json=row["trace_context_json"],
            version=row["version"],
            context_closed=bool(row["context_closed"]),
            tokens_used=row["tokens_used"],
        )


def encode_cursor(data: dict[str, Any]) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor("malformed cursor") from exc
    if not isinstance(data, dict):
        raise InvalidCursor("malformed cursor")
    return data


def _task_view(row: aiosqlite.Row) -> TaskView:
    return TaskView(
        id=row["id"],
        agent_id=row["agent_id"],
        task_type=row["task_type"],
        status=row["status"],
        status_reason=row["status_reason"],
        attempt=row["attempt"],
        deadline=row["deadline"],
        finding_ids=json.loads(row["finding_ids_json"] or "[]"),
        usage=json.loads(row["usage_json"]) if row["usage_json"] else None,
        error_category=row["error_category"],
        llm_mode=row["llm_mode"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _investigation(row: aiosqlite.Row, tasks: Sequence[TaskView] = ()) -> Investigation:
    return Investigation(
        id=row["id"],
        scenario_id=row["scenario_id"],
        objective=row["objective"],
        status=row["status"],
        status_reason=row["status_reason"],
        tokens_used=row["tokens_used"],
        token_budget=row["token_budget"],
        deadline=row["deadline"],
        assessment_id=row["assessment_id"],
        tasks=list(tasks),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event(row: aiosqlite.Row) -> TimelineEvent:
    return TimelineEvent(
        seq=row["seq"],
        investigation_id=row["investigation_id"],
        type=row["type"],
        payload=json.loads(row["payload_json"]),
        created_at=row["created_at"],
    )


class Repository:
    def __init__(self, db: Database, broker: TimelineBroker) -> None:
        self.db = db
        self.broker = broker

    # -- writes (inside a caller-owned transaction) -------------------------------------------

    async def count_active(self, tx: Transaction) -> int:
        marks = ",".join("?" * len(ACTIVE_STATUSES))
        row = await tx.fetchone(
            f"SELECT COUNT(*) AS n FROM investigations WHERE status IN ({marks})",
            [s.value for s in ACTIVE_STATUSES],
        )
        return int(row["n"]) if row else 0

    async def insert_investigation(
        self,
        tx: Transaction,
        *,
        investigation_id: str,
        scenario_id: str,
        objective: str,
        token_budget: int,
        deadline: datetime,
        trace_context_json: str | None,
        now: datetime,
    ) -> Investigation:
        await tx.execute(
            """INSERT INTO investigations
               (id, scenario_id, objective, status, status_reason, tokens_used, token_budget,
                deadline, assessment_id, context_closed, trace_context_json,
                created_at, updated_at, version)
               VALUES (?, ?, ?, ?, NULL, 0, ?, ?, NULL, 0, ?, ?, ?, 1)""",
            (
                investigation_id,
                scenario_id,
                objective,
                InvestigationStatus.CREATED.value,
                token_budget,
                iso(deadline),
                trace_context_json,
                iso(now),
                iso(now),
            ),
        )
        row = await tx.fetchone("SELECT * FROM investigations WHERE id = ?", (investigation_id,))
        assert row is not None
        return _investigation(row)

    async def append_event(
        self,
        tx: Transaction,
        investigation_id: str,
        event_type: TimelineEventType,
        payload: dict[str, Any],
        now: datetime,
    ) -> TimelineEvent:
        """Insert a timeline row; publish it to SSE subscribers only after commit (D3)."""
        cur = await tx.execute(
            """INSERT INTO timeline_events (investigation_id, type, payload_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (investigation_id, event_type.value, json.dumps(payload), iso(now)),
        )
        event = TimelineEvent(
            seq=cur.lastrowid,
            investigation_id=investigation_id,
            type=event_type,
            payload=payload,
            created_at=now,
        )
        tx.after_commit(lambda: self.broker.publish(event))
        return event

    # -- S2: tasks and guarded transitions (ADR-008 F2-F4) --------------------------------------

    async def insert_task(
        self,
        tx: Transaction,
        *,
        task_id: str,
        investigation_id: str,
        agent_id: AgentId,
        task_type: str,
        deadline: datetime,
        token_reservation: int,
        now: datetime,
        state: OrchestratorTaskState = OrchestratorTaskState.PENDING,
    ) -> None:
        """Insert attempt 1 (PENDING = dispatch intent, F2) plus its ``task.status_changed``."""
        check_task_insert(state)
        await tx.execute(
            """INSERT INTO tasks
               (id, investigation_id, agent_id, task_type, status, status_reason, attempt,
                deadline, token_reservation, acked_at, finding_ids_json, usage_json,
                error_category, error_code, created_at, updated_at, version, llm_mode)
               VALUES (?, ?, ?, ?, ?, NULL, 1, ?, ?, NULL, '[]', NULL, NULL, NULL,
                       ?, ?, 1, NULL)""",
            (
                task_id,
                investigation_id,
                agent_id.value,
                task_type,
                state.value,
                iso(deadline),
                token_reservation,
                iso(now),
                iso(now),
            ),
        )
        payload = TaskStatusChanged(
            task_id=task_id, agent_id=agent_id, attempt=1, from_state=None, to_state=state
        ).model_dump(mode="json")
        await self.append_event(
            tx, investigation_id, TimelineEventType.TASK_STATUS_CHANGED, payload, now
        )

    async def transition_task(
        self,
        tx: Transaction,
        expected: TaskRecord,
        to: OrchestratorTaskState,
        *,
        now: datetime,
        reason: ErrorCode | None = None,
        fields: dict[str, Any] | None = None,
    ) -> Applied:
        """Guarded task status write (ADR-008 F3-F4).

        ``expected`` is usually read before a network call (poll/dispatch), so another writer may
        have moved the row meanwhile. The row is therefore re-read inside the caller's write
        transaction and the edge is validated against the **stored** state, never the snapshot:
        already there → ``NOOP`` (no event); edge not allowed → ``DROPPED`` (+ a
        ``transition.dropped`` note); else update ``WHERE version = <stored version>`` and emit
        one ``task.status_changed``."""
        row = await tx.fetchone("SELECT * FROM tasks WHERE id = ?", (expected.id,))
        assert row is not None, expected.id
        current = TaskRecord.from_row(row)
        if current.version != expected.version:
            log.info(
                "task version conflict; re-validating against the stored row",
                extra={
                    "task_id": current.id,
                    "expected_version": expected.version,
                    "stored_version": current.version,
                },
            )
        if current.status is to:
            return Applied.NOOP
        if not task_allowed(current.status, to, src_reason=current.status_reason):
            await self._dropped(
                tx, current.investigation_id, "task", current.id, current.status, to, reason, now
            )
            return Applied.DROPPED
        fields = dict(fields or {})
        if isinstance(fields.get("deadline"), datetime):
            fields["deadline"] = iso(fields["deadline"])  # the column and the event take ISO text
        attempt = current.attempt
        if to is OrchestratorTaskState.PENDING:
            # FR-122, enforced at the write: a re-dispatch is attempt + 1, and there is only one.
            attempt = fields.get("attempt", current.attempt)
            if attempt != current.attempt + 1 or attempt > MAX_ATTEMPTS:
                raise IllegalTransition(
                    f"task {current.id} {current.status} -> PENDING needs attempt "
                    f"{current.attempt + 1} <= {MAX_ATTEMPTS}, got {attempt}"
                )
        values: dict[str, Any] = {
            "status": to.value,
            "status_reason": reason.value if reason else None,
            "updated_at": iso(now),
            **fields,
        }
        assignments = ", ".join(f"{col} = ?" for col in values)
        cur = await tx.execute(
            f"UPDATE tasks SET {assignments}, version = version + 1 WHERE id = ? AND version = ?",
            [*values.values(), current.id, current.version],
        )
        assert cur.rowcount == 1  # same write transaction as the read above
        payload = TaskStatusChanged(
            task_id=current.id,
            agent_id=current.agent_id,
            attempt=attempt,
            from_state=current.status,
            to_state=to,
            reason=reason,
            deadline=parse_iso(fields["deadline"]) if "deadline" in fields else None,
        ).model_dump(mode="json")
        await self.append_event(
            tx, current.investigation_id, TimelineEventType.TASK_STATUS_CHANGED, payload, now
        )
        return Applied.APPLIED

    async def transition_investigation(
        self,
        tx: Transaction,
        investigation_id: str,
        to: InvestigationStatus,
        *,
        now: datetime,
        reason: ErrorCode | None = None,
        skipped_agents: Sequence[AgentId] = (),
    ) -> Applied:
        """Guarded investigation status write plus its ``investigation.status_changed`` event.

        The row is read inside the caller's write transaction and updated with
        ``WHERE version = <read version>``, so the edge is validated against the stored state."""
        row = await tx.fetchone(
            "SELECT status, version FROM investigations WHERE id = ?", (investigation_id,)
        )
        assert row is not None, investigation_id
        current = InvestigationStatus(row["status"])
        if current is to:
            return Applied.NOOP
        if not investigation_allowed(current, to):
            await self._dropped(
                tx, investigation_id, "investigation", investigation_id, current, to, reason, now
            )
            return Applied.DROPPED
        cur = await tx.execute(
            """UPDATE investigations SET status = ?, status_reason = ?, updated_at = ?,
               version = version + 1 WHERE id = ? AND version = ?""",
            (
                to.value,
                reason.value if reason else None,
                iso(now),
                investigation_id,
                row["version"],
            ),
        )
        assert cur.rowcount == 1  # same transaction as the read above
        payload = InvestigationStatusChanged(
            from_status=current, to_status=to, reason=reason, skipped_agents=list(skipped_agents)
        ).model_dump(mode="json")
        await self.append_event(
            tx, investigation_id, TimelineEventType.INVESTIGATION_STATUS_CHANGED, payload, now
        )
        return Applied.APPLIED

    async def add_tokens_used(
        self, tx: Transaction, investigation_id: str, tokens: int, now: datetime
    ) -> None:
        if tokens <= 0:
            return
        await tx.execute(
            """UPDATE investigations SET tokens_used = tokens_used + ?, updated_at = ?,
               version = version + 1 WHERE id = ?""",
            (tokens, iso(now), investigation_id),
        )

    async def set_context_closed(
        self, tx: Transaction, investigation_id: str, closed: bool, now: datetime
    ) -> None:
        """Record whether the Context Service close landed (``investigations.context_closed``)."""
        await tx.execute(
            """UPDATE investigations SET context_closed = ?, updated_at = ?,
               version = version + 1 WHERE id = ?""",
            (int(closed), iso(now), investigation_id),
        )

    async def _dropped(
        self,
        tx: Transaction,
        investigation_id: str,
        entity: str,
        entity_id: str,
        current: StrEnum,
        to: StrEnum,
        reason: ErrorCode | None,
        now: datetime,
    ) -> None:
        log.warning(
            "transition dropped",
            extra={"entity": entity, "id": entity_id, "from": current.value, "to": to.value},
        )
        await self.append_event(
            tx,
            investigation_id,
            TimelineEventType.TRANSITION_DROPPED,
            {
                "entity": entity,
                "id": entity_id,
                "current_state": current.value,
                "attempted_state": to.value,
                "reason": reason.value if reason else None,
            },
            now,
        )

    async def append_note(
        self,
        tx: Transaction,
        investigation_id: str,
        event_type: TimelineEventType,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        await self.append_event(tx, investigation_id, event_type, payload, now)

    async def task_records(self, investigation_id: str) -> list[TaskRecord]:
        rows = await self.db.fetchall(
            "SELECT * FROM tasks WHERE investigation_id = ? ORDER BY created_at, id",
            (investigation_id,),
        )
        return [TaskRecord.from_row(r) for r in rows]

    async def tx_task_records(self, tx: Transaction, investigation_id: str) -> list[TaskRecord]:
        async with tx.conn.execute(
            "SELECT * FROM tasks WHERE investigation_id = ? ORDER BY created_at, id",
            (investigation_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [TaskRecord.from_row(r) for r in rows]

    async def investigation_record(self, investigation_id: str) -> InvestigationRecord | None:
        row = await self.db.fetchone(
            "SELECT * FROM investigations WHERE id = ?", (investigation_id,)
        )
        return InvestigationRecord.from_row(row) if row else None

    async def tx_investigation_record(
        self, tx: Transaction, investigation_id: str
    ) -> InvestigationRecord | None:
        row = await tx.fetchone("SELECT * FROM investigations WHERE id = ?", (investigation_id,))
        return InvestigationRecord.from_row(row) if row else None

    # -- reads ---------------------------------------------------------------------------------

    async def get_investigation(self, investigation_id: str) -> Investigation | None:
        row = await self.db.fetchone(
            "SELECT * FROM investigations WHERE id = ?", (investigation_id,)
        )
        if row is None:
            return None
        tasks = await self._tasks_by_investigation([investigation_id])
        return _investigation(row, tasks.get(investigation_id, []))

    async def get_trace_context(self, investigation_id: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT trace_context_json FROM investigations WHERE id = ?", (investigation_id,)
        )
        return row["trace_context_json"] if row else None

    async def investigation_exists(self, investigation_id: str) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM investigations WHERE id = ?", (investigation_id,)
        )
        return row is not None

    async def list_investigations(
        self, limit: int, cursor: str | None
    ) -> tuple[list[Investigation], str | None]:
        """Newest first, keyset on ``(created_at, id)``."""
        params: list[Any] = []
        where = ""
        if cursor:
            data = decode_cursor(cursor)
            if not isinstance(data.get("c"), str) or not isinstance(data.get("i"), str):
                raise InvalidCursor("malformed cursor")
            where = "WHERE (created_at, id) < (?, ?)"
            params += [data["c"], data["i"]]
        rows = await self.db.fetchall(
            f"SELECT * FROM investigations {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            [*params, limit + 1],
        )
        page, more = rows[:limit], len(rows) > limit
        tasks = await self._tasks_by_investigation([r["id"] for r in page])
        items = [_investigation(r, tasks.get(r["id"], [])) for r in page]
        next_cursor = (
            encode_cursor({"c": page[-1]["created_at"], "i": page[-1]["id"]}) if more else None
        )
        return items, next_cursor

    async def list_ids_with_status(self, status: InvestigationStatus) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT id FROM investigations WHERE status = ? ORDER BY created_at", (status.value,)
        )
        return [r["id"] for r in rows]

    async def list_unclosed_terminal(self) -> list[tuple[str, InvestigationStatus]]:
        """Terminal investigations whose Context Service close has not landed (FR-126)."""
        terminal = [s.value for s in InvestigationStatus if s.is_terminal]
        marks = ",".join("?" * len(terminal))
        rows = await self.db.fetchall(
            f"""SELECT id, status FROM investigations
                WHERE context_closed = 0 AND status IN ({marks}) ORDER BY created_at""",
            tuple(terminal),
        )
        return [(r["id"], InvestigationStatus(r["status"])) for r in rows]

    async def events_after(
        self, investigation_id: str, after_seq: int, limit: int
    ) -> list[TimelineEvent]:
        rows = await self.db.fetchall(
            """SELECT * FROM timeline_events WHERE investigation_id = ? AND seq > ?
               ORDER BY seq LIMIT ?""",
            (investigation_id, after_seq, limit),
        )
        return [_event(r) for r in rows]

    async def timeline_page(
        self, investigation_id: str, limit: int, cursor: str | None
    ) -> tuple[list[TimelineEvent], str | None]:
        after = 0
        if cursor:
            data = decode_cursor(cursor)
            if not isinstance(data.get("s"), int) or data["s"] < 0:
                raise InvalidCursor("malformed cursor")
            after = data["s"]
        events = await self.events_after(investigation_id, after, limit + 1)
        page, more = events[:limit], len(events) > limit
        return page, (encode_cursor({"s": page[-1].seq}) if more else None)

    async def _tasks_by_investigation(self, ids: list[str]) -> dict[str, list[TaskView]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = await self.db.fetchall(
            f"SELECT * FROM tasks WHERE investigation_id IN ({marks}) ORDER BY created_at, id",
            ids,
        )
        out: dict[str, list[TaskView]] = {}
        for r in rows:
            out.setdefault(r["investigation_id"], []).append(_task_view(r))
        return out
