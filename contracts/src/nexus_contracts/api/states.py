"""Orchestrator-owned lifecycle enums (spec §8.2, §8.7; ADR-004 C2).

These are stored in orchestrator SQLite and exposed by the frontend API (§12.1 "details
including task states"). They are **not** the agent wire enum: agents report
``nexus_contracts.a2a.AgentTaskStatus`` (A2A names, ``CANCELED``), while the orchestrator keeps
``CANCELLED``. The only crossing point is ``nexus_contracts.a2a.status_mapping``.

Transition tables live in ``apps/orchestrator/workflow/`` (§7). Only the vocabulary is shared.
"""

from enum import StrEnum


class InvestigationStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    ANALYZING = "ANALYZING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.PARTIAL,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELLED,
        }


class OrchestratorTaskState(StrEnum):
    """What the orchestrator knows about a task (§8.2), including ``PENDING``/``TIMED_OUT``."""

    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Terminal from the poller's view. ``TIMED_OUT`` and a re-dispatchable ``FAILED`` may
        still go back to ``PENDING`` once (§8.2, §8.5); that rule belongs to the workflow."""
        return self in {
            OrchestratorTaskState.COMPLETED,
            OrchestratorTaskState.FAILED,
            OrchestratorTaskState.TIMED_OUT,
            OrchestratorTaskState.CANCELLED,
        }
