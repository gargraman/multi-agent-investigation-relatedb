"""Stable machine-readable error codes and categories (spec §8.2, §8.3, §8.5, §13.2, §17.1).

``ErrorCode`` lists every code the spec and ADRs name (spec v1.3 added ``content_conflict``,
``replay_miss`` and ``analysis_policy_unmet``; a malformed body is ``schema_invalid``). Used:

- in ``TaskError.code`` (agent ``FAILED``/``REJECTED`` results, §12.2),
- as the orchestrator ``tasks.error_code`` / ``status_reason`` (§8.7),
- as ``ProblemDetails.code`` on 409/422 responses. ``ProblemDetails.code`` stays ``str`` so the
  RFC 7807 model remains open; services should still only emit values from this enum.

``ErrorCategory`` is **derived**: the spec shows only ``{"category":"llm","code":"schema_invalid"}``
(§12.2) and defines no category list. The grouping in ``CATEGORY_BY_CODE`` is a proposal pending
a spec update.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    # Agent task failures (§8.2 FAILED list, §17.1)
    SCHEMA_INVALID = "schema_invalid"
    LLM_UNAVAILABLE = "llm_unavailable"
    EVIDENCE_INTEGRITY_ERROR = "evidence_integrity_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    AGENT_RESTARTED = "agent_restarted"
    UNKNOWN_FINDING_REFERENCE = "unknown_finding_reference"
    # Orchestrator-assigned task reasons (§8.2 mapping table)
    TASK_LOST = "task_lost"
    TASK_REJECTED = "task_rejected"
    # Investigation status reason (§8.2)
    INVESTIGATION_DEADLINE = "investigation_deadline"
    # HTTP 409 / 422 problem codes (§8.3, §8.5, §8.6, §13.2)
    STALE_ATTEMPT = "stale_attempt"
    ALREADY_COMMITTED = "already_committed"
    INVESTIGATION_CANCELLED = "investigation_cancelled"
    REPLAY_OBJECTIVE_LOCKED = "replay_objective_locked"
    CONTENT_CONFLICT = "content_conflict"
    # Added in spec v1.3
    REPLAY_MISS = "replay_miss"
    ANALYSIS_POLICY_UNMET = "analysis_policy_unmet"


class ErrorCategory(StrEnum):
    LLM = "llm"
    EVIDENCE = "evidence"
    BUDGET = "budget"
    DEADLINE = "deadline"
    LIFECYCLE = "lifecycle"
    POLICY = "policy"
    CONFLICT = "conflict"
    VALIDATION = "validation"


CATEGORY_BY_CODE: dict[ErrorCode, ErrorCategory] = {
    ErrorCode.SCHEMA_INVALID: ErrorCategory.LLM,
    ErrorCode.LLM_UNAVAILABLE: ErrorCategory.LLM,
    ErrorCode.UNKNOWN_FINDING_REFERENCE: ErrorCategory.LLM,
    ErrorCode.EVIDENCE_INTEGRITY_ERROR: ErrorCategory.EVIDENCE,
    ErrorCode.BUDGET_EXCEEDED: ErrorCategory.BUDGET,
    ErrorCode.DEADLINE_EXCEEDED: ErrorCategory.DEADLINE,
    ErrorCode.INVESTIGATION_DEADLINE: ErrorCategory.DEADLINE,
    ErrorCode.AGENT_RESTARTED: ErrorCategory.LIFECYCLE,
    ErrorCode.TASK_LOST: ErrorCategory.LIFECYCLE,
    ErrorCode.INVESTIGATION_CANCELLED: ErrorCategory.LIFECYCLE,
    ErrorCode.TASK_REJECTED: ErrorCategory.POLICY,
    ErrorCode.STALE_ATTEMPT: ErrorCategory.CONFLICT,
    ErrorCode.ALREADY_COMMITTED: ErrorCategory.CONFLICT,
    ErrorCode.REPLAY_OBJECTIVE_LOCKED: ErrorCategory.VALIDATION,
    ErrorCode.CONTENT_CONFLICT: ErrorCategory.CONFLICT,
    ErrorCode.REPLAY_MISS: ErrorCategory.LLM,
    ErrorCode.ANALYSIS_POLICY_UNMET: ErrorCategory.POLICY,
}
"""Every code has exactly one category (tested exhaustively)."""
