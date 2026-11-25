"""Repository over the §8.7 tables. Reads use the read connection; writes take a ``Transaction``.

Every row is mapped to its contract model on the way out, so a bad row fails loudly here rather
than leaking to the API.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import aiosqlite

from nexus_contracts.api.investigations import Investigation, TaskView
from nexus_contracts.api.states import InvestigationStatus
from nexus_contracts.events.timeline import TimelineEvent, TimelineEventType
from nexus_orchestrator.ids import iso
from nexus_orchestrator.pubsub import TimelineBroker
from nexus_orchestrator.state.db import Database, Transaction

ACTIVE_STATUSES = (
    InvestigationStatus.CREATED,
    InvestigationStatus.RUNNING,
    InvestigationStatus.ANALYZING,
)


class InvalidCursor(ValueError):
    pass


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
