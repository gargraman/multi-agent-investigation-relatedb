"""Agent task store: SQLite per S2 spec §8.3 (ADR-003 B4/B5, NFR-005).

- ``aiosqlite`` (no blocking call on the loop), WAL, ``busy_timeout=5000``.
- One connection, every statement behind one ``asyncio.Lock`` (single writer).
- Dedup on ``(task_id, attempt)`` is decided inside one ``BEGIN IMMEDIATE`` transaction.
- Every update is compare-and-set on ``version``. The one exception is the ``cancel_requested``
  flag: a monotonic 0 -> 1 marker set only on a non-terminal row, which does not bump ``version``
  so the runner's in-flight compare-and-set is unaffected by a cancel request.
- Boot sweep (FR-024): SUBMITTED/WORKING become FAILED(``agent_restarted``).
- One store (one SQLite file) per agent: rows carry no agent column; every row and
  ``TaskResult`` reports the store's ``agent_id``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiosqlite

from nexus_contracts.a2a.task import (
    AgentTaskStatus,
    TaskError,
    TaskRequest,
    TaskResult,
    TaskUsage,
)
from nexus_contracts.api.errors import CATEGORY_BY_CODE, ErrorCode
from nexus_contracts.common import AgentId, LlmMode

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tasks (
  task_id            TEXT    NOT NULL,
  attempt            INTEGER NOT NULL CHECK (attempt >= 1),
  investigation_id   TEXT    NOT NULL,
  scenario_id        TEXT    NOT NULL,
  task_type          TEXT    NOT NULL,
  status             TEXT    NOT NULL CHECK (status IN
                       ('SUBMITTED','WORKING','COMPLETED','FAILED','CANCELED','REJECTED')),
  request_hash       TEXT    NOT NULL,
  deadline           TEXT    NOT NULL,
  token_budget       INTEGER NOT NULL,
  input_tokens       INTEGER NOT NULL DEFAULT 0,
  output_tokens      INTEGER NOT NULL DEFAULT 0,
  llm_calls          INTEGER NOT NULL DEFAULT 0,
  usage_replayed     INTEGER NOT NULL DEFAULT 0 CHECK (usage_replayed IN (0,1)),
  llm_mode           TEXT    NOT NULL CHECK (llm_mode IN ('live','replay')),
  model_id           TEXT,
  prompt_version     TEXT,
  summary            TEXT,
  finding_ids_json   TEXT    NOT NULL DEFAULT '[]',
  warnings_json      TEXT    NOT NULL DEFAULT '[]',
  error_category     TEXT,
  error_code         TEXT,
  cancel_requested   INTEGER NOT NULL DEFAULT 0,
  trace_context_json TEXT,
  created_at         TEXT    NOT NULL,
  updated_at         TEXT    NOT NULL,
  completed_at       TEXT,
  version            INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (task_id, attempt),
  CHECK ((status IN ('FAILED','REJECTED')) = (error_code IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_agent_tasks_status ON agent_tasks(status);
"""

NON_TERMINAL = ("SUBMITTED", "WORKING")

BOOT_SWEEP = """
UPDATE agent_tasks
   SET status='FAILED', error_category=:cat, error_code='agent_restarted',
       completed_at=:now, updated_at=:now, version=version+1
 WHERE status IN ('SUBMITTED','WORKING')
"""

_UPDATABLE = frozenset(
    {
        "status",
        "input_tokens",
        "output_tokens",
        "llm_calls",
        "usage_replayed",
        "model_id",
        "prompt_version",
        "summary",
        "finding_ids_json",
        "warnings_json",
        "error_category",
        "error_code",
        "completed_at",
    }
)


def utcnow() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


HASH_EXCLUDED_FIELDS = frozenset({"trace_context", "deadline"})
"""Per-try fields left out of the dedup hash: the orchestrator re-POSTs the same task after a
restart/retry with a fresh ``trace_context`` and a recomputed ``deadline``; that must be a
duplicate, not a conflict (S3)."""


def request_hash(request: TaskRequest) -> str:
    """sha256 of the canonical TaskRequest JSON minus :data:`HASH_EXCLUDED_FIELDS` (sorted keys,
    no whitespace, UTF-8)."""
    body = json.dumps(
        request.model_dump(mode="json", exclude=set(HASH_EXCLUDED_FIELDS)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class SubmitDecision(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"  # same (task_id, attempt), same hash -> 200 current status
    CONFLICT = "conflict"  # same pair, different hash -> 409 content_conflict
    STALE = "stale"  # attempt lower than the max stored -> 409 stale_attempt


@dataclass(frozen=True)
class TaskRow:
    data: dict[str, Any]
    agent_id: AgentId

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def status(self) -> AgentTaskStatus:
        return AgentTaskStatus(self.data["status"])

    @property
    def version(self) -> int:
        return int(self.data["version"])

    def to_result(self) -> TaskResult:
        d = self.data
        status = self.status
        error = TaskError.for_code(ErrorCode(d["error_code"])) if d["error_code"] else None
        return TaskResult(
            task_id=d["task_id"],
            agent_id=self.agent_id,
            status=status,
            attempt=d["attempt"],
            llm_mode=LlmMode(d["llm_mode"]),
            summary=d["summary"],
            finding_ids=json.loads(d["finding_ids_json"])
            if status is AgentTaskStatus.COMPLETED
            else [],
            warnings=json.loads(d["warnings_json"]),
            usage=TaskUsage(
                input_tokens=d["input_tokens"],
                output_tokens=d["output_tokens"],
                llm_calls=d["llm_calls"],
                replayed=bool(d["usage_replayed"]),
            ),
            model_id=d["model_id"],
            prompt_version=d["prompt_version"],
            completed_at=d["completed_at"] if status.is_terminal else None,
            error=error,
        )


class TaskStore:
    def __init__(self, path: Path, agent_id: AgentId) -> None:
        self.path = path
        self.agent_id = agent_id
        self._conn: aiosqlite.Connection | None = None
        self._lock: asyncio.Lock | None = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    async def open(self) -> int:
        """Open, create the schema, run the boot sweep. Returns the number of swept rows."""
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        for pragma in PRAGMAS:
            await conn.execute(pragma)
        await conn.executescript(SCHEMA)
        self._conn = conn
        async with self._tx() as c:
            cur = await c.execute(
                BOOT_SWEEP,
                {"cat": CATEGORY_BY_CODE[ErrorCode.AGENT_RESTARTED].value, "now": utcnow()},
            )
            return cur.rowcount

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        assert self._conn is not None and self._lock is not None, "store not open"
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                await self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    await self._conn.execute("ROLLBACK")
                raise

    async def _one(self, conn: aiosqlite.Connection, sql: str, params: tuple) -> TaskRow | None:
        async with conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return TaskRow(dict(row), self.agent_id) if row is not None else None

    async def get(self, task_id: str, attempt: int) -> TaskRow | None:
        async with self._tx() as conn:
            return await self._one(
                conn, "SELECT * FROM agent_tasks WHERE task_id=? AND attempt=?", (task_id, attempt)
            )

    async def latest(self, task_id: str) -> TaskRow | None:
        """Row of the highest attempt (FR-022)."""
        async with self._tx() as conn:
            return await self._one(
                conn,
                "SELECT * FROM agent_tasks WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
                (task_id,),
            )

    async def submit(
        self,
        request: TaskRequest,
        *,
        llm_mode: LlmMode,
        rejected: ErrorCode | None = None,
    ) -> tuple[SubmitDecision, TaskRow]:
        """Dedup and insert in one transaction (FR-020). A new row is SUBMITTED, or REJECTED
        when ``rejected`` is given."""
        rhash = request_hash(request)
        async with self._tx() as conn:
            latest = await self._one(
                conn,
                "SELECT * FROM agent_tasks WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
                (request.task_id,),
            )
            if latest is not None:
                if request.attempt < latest["attempt"]:
                    return SubmitDecision.STALE, latest
                if request.attempt == latest["attempt"]:
                    if latest["request_hash"] == rhash:
                        return SubmitDecision.DUPLICATE, latest
                    return SubmitDecision.CONFLICT, latest
            now = utcnow()
            status = AgentTaskStatus.REJECTED if rejected else AgentTaskStatus.SUBMITTED
            await conn.execute(
                """INSERT INTO agent_tasks (task_id, attempt, investigation_id, scenario_id,
                   task_type, status, request_hash, deadline, token_budget, llm_mode,
                   error_category, error_code, trace_context_json, created_at, updated_at,
                   completed_at, summary)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    request.task_id,
                    request.attempt,
                    request.investigation_id,
                    request.scenario_id,
                    request.task_type,
                    status.value,
                    rhash,
                    request.deadline.isoformat().replace("+00:00", "Z"),
                    request.token_budget,
                    llm_mode.value,
                    CATEGORY_BY_CODE[rejected].value if rejected else None,
                    rejected.value if rejected else None,
                    request.trace_context.model_dump_json(),
                    now,
                    now,
                    now if rejected else None,
                    "Task rejected by agent policy." if rejected else None,
                ),
            )
            row = await self._one(
                conn,
                "SELECT * FROM agent_tasks WHERE task_id=? AND attempt=?",
                (request.task_id, request.attempt),
            )
            assert row is not None
            return SubmitDecision.NEW, row

    async def update(
        self, task_id: str, attempt: int, expected_version: int, **fields: Any
    ) -> TaskRow | None:
        """Compare-and-set update. Returns the new row, or ``None`` if the version moved."""
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"not updatable: {sorted(bad)}")
        cols = ", ".join(f"{k}=?" for k in fields)
        async with self._tx() as conn:
            cur = await conn.execute(
                f"UPDATE agent_tasks SET {cols}, updated_at=?, version=version+1 "
                "WHERE task_id=? AND attempt=? AND version=?",
                (*fields.values(), utcnow(), task_id, attempt, expected_version),
            )
            if cur.rowcount != 1:
                return None
            return await self._one(
                conn, "SELECT * FROM agent_tasks WHERE task_id=? AND attempt=?", (task_id, attempt)
            )

    async def request_cancel(self, task_id: str) -> TaskRow | None:
        """Set ``cancel_requested`` on the latest attempt if it is not terminal (S3 FR-102).
        Returns the latest row (``None`` for an unknown task). Idempotent; never touches a
        terminal row, and does not bump ``version`` (see module docstring)."""
        async with self._tx() as conn:
            await conn.execute(
                "UPDATE agent_tasks SET cancel_requested=1, updated_at=? "
                "WHERE task_id=? AND cancel_requested=0 AND status IN (?,?) "
                "AND attempt=(SELECT MAX(attempt) FROM agent_tasks WHERE task_id=?)",
                (utcnow(), task_id, *NON_TERMINAL, task_id),
            )
            return await self._one(
                conn,
                "SELECT * FROM agent_tasks WHERE task_id=? ORDER BY attempt DESC LIMIT 1",
                (task_id,),
            )

    async def is_cancel_requested(self, task_id: str, attempt: int) -> bool:
        async with self._tx() as conn:
            row = await self._one(
                conn,
                "SELECT cancel_requested FROM agent_tasks WHERE task_id=? AND attempt=?",
                (task_id, attempt),
            )
        return row is not None and bool(row["cancel_requested"])

    async def supersede_lower_attempts(self, task_id: str, attempt: int) -> list[int]:
        """A higher attempt supersedes running lower ones: every non-terminal attempt below
        ``attempt`` becomes CANCELED (version-guarded, with ``cancel_requested`` set). Returns the
        superseded attempt numbers."""
        now = utcnow()
        done: list[int] = []
        async with self._tx() as conn:
            async with conn.execute(
                "SELECT attempt, version FROM agent_tasks "
                "WHERE task_id=? AND attempt<? AND status IN (?,?)",
                (task_id, attempt, *NON_TERMINAL),
            ) as cur:
                rows = await cur.fetchall()
            for lower, version in rows:
                cur = await conn.execute(
                    "UPDATE agent_tasks SET status='CANCELED', cancel_requested=1, "
                    "summary='Task canceled: superseded by a higher attempt.', "
                    "completed_at=?, updated_at=?, version=version+1 "
                    "WHERE task_id=? AND attempt=? AND version=?",
                    (now, now, task_id, lower, version),
                )
                if cur.rowcount == 1:
                    done.append(lower)
        return done
