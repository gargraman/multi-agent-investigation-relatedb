"""Explicit transition tables: the single source of allowed state changes (spec §8.2, ADR-008).

Every status write in the repository is checked here first. An edge that isn't listed is
rejected (``IllegalTransition``) or, for a writer that lost a race, dropped with a
``transition.dropped`` timeline note (ADR-008 F4). Terminal states have no outgoing edges, so
they are sticky (§8.2), except for the single re-dispatch edge below.

The tables are the full §8.2 machines. S2 uses only this subset:

- Investigation: CREATED→RUNNING, RUNNING→PARTIAL, RUNNING→FAILED. With no enabled template
  agent it goes CREATED→RUNNING→FAILED in one transaction (see ``workflow/terminal.py``).
- Task: insert PENDING, PENDING→DISPATCHED, PENDING→FAILED, DISPATCHED→RUNNING/COMPLETED/FAILED/
  CANCELLED, RUNNING→COMPLETED/FAILED/CANCELLED.

The re-dispatch edges (TIMED_OUT→PENDING, FAILED(agent_restarted|task_lost)→PENDING) and
TIMED_OUT itself belong to S3; they are listed so the table stays complete, and guarded.
"""

from __future__ import annotations

from nexus_contracts.a2a.status_mapping import REDISPATCHABLE_CODES
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.states import InvestigationStatus as I
from nexus_contracts.api.states import OrchestratorTaskState as T


class IllegalTransition(ValueError):
    pass


INVESTIGATION_TRANSITIONS: dict[I, frozenset[I]] = {
    I.CREATED: frozenset({I.RUNNING, I.FAILED, I.CANCELLED}),
    I.RUNNING: frozenset({I.ANALYZING, I.PARTIAL, I.FAILED, I.CANCELLED}),
    I.ANALYZING: frozenset({I.COMPLETED, I.PARTIAL, I.FAILED, I.CANCELLED}),
    I.COMPLETED: frozenset(),
    I.PARTIAL: frozenset(),
    I.FAILED: frozenset(),
    I.CANCELLED: frozenset(),
}

INITIAL_INVESTIGATION_STATUS = I.CREATED

TASK_TRANSITIONS: dict[T, frozenset[T]] = {
    T.PENDING: frozenset({T.DISPATCHED, T.FAILED, T.TIMED_OUT, T.CANCELLED}),
    T.DISPATCHED: frozenset({T.RUNNING, T.COMPLETED, T.FAILED, T.TIMED_OUT, T.CANCELLED}),
    T.RUNNING: frozenset({T.COMPLETED, T.FAILED, T.TIMED_OUT, T.CANCELLED}),
    T.COMPLETED: frozenset(),
    T.FAILED: frozenset({T.PENDING}),  # only for a re-dispatchable reason, see below
    T.TIMED_OUT: frozenset({T.PENDING}),
    T.CANCELLED: frozenset(),
}

# G1 (S2 review): only enabled agents get rows, and they start PENDING. No phantom FAILED rows.
INITIAL_TASK_STATES: frozenset[T] = frozenset({T.PENDING})


def investigation_allowed(src: I, dst: I) -> bool:
    return dst in INVESTIGATION_TRANSITIONS[src]


def check_investigation(src: I, dst: I) -> None:
    if not investigation_allowed(src, dst):
        raise IllegalTransition(f"investigation {src} -> {dst} is not allowed")


def task_allowed(src: T, dst: T, *, src_reason: ErrorCode | str | None = None) -> bool:
    if dst not in TASK_TRANSITIONS[src]:
        return False
    if src is T.FAILED and dst is T.PENDING:
        return src_reason is not None and ErrorCode(src_reason) in REDISPATCHABLE_CODES
    return True


def check_task(src: T, dst: T, *, src_reason: ErrorCode | str | None = None) -> None:
    if not task_allowed(src, dst, src_reason=src_reason):
        raise IllegalTransition(f"task {src} -> {dst} is not allowed (reason={src_reason})")


def check_task_insert(state: T) -> None:
    if state not in INITIAL_TASK_STATES:
        raise IllegalTransition(f"a task cannot be inserted as {state}")
