"""The agent plug-in contract (S3a): everything agent-specific the shared runtime needs.

The runtime (:mod:`nexus_shared.agent.app`, ``.api``, ``.runner``) owns HTTP, auth, the task store,
deadlines, the Context Service submit and telemetry. An :class:`AgentSpec` owns the rest: its
identity and task types, its submit-time policy check and its pure pipeline, which turns one
accepted ``TaskRequest`` into one :class:`PipelineOutcome`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from opentelemetry import trace

from nexus_contracts.a2a.task import AgentTaskStatus, TaskError, TaskRequest, TaskUsage
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.api.findings import FindingBatchRequest
from nexus_contracts.common import AgentId
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_shared.llm import LLMProvider, TokenBudget


@dataclass(frozen=True)
class PipelineOutcome:
    """Result of one pipeline run.

    ``status`` is COMPLETED, FAILED or REJECTED (REJECTED only for requests that fail
    :func:`validate_request`; the API normally rejects those at submit). ``batch`` is ``None``
    when there are zero candidates or the run did not complete. ``error`` is a ``TaskError``
    (``.code`` / ``.category``) on FAILED/REJECTED."""

    status: AgentTaskStatus
    batch: FindingBatchRequest | None
    usage: TaskUsage
    model_id: str | None
    prompt_version: str
    error: TaskError | None
    summary: str
    warnings: list[str] = field(default_factory=list)
    candidate_count: int = 0


@runtime_checkable
class AgentSpec(Protocol):
    agent_id: AgentId
    task_types: tuple[str, ...]
    role: str  # agent card text
    version: str  # agent package version (agent card, OpenAPI)

    def validate_request(
        self, request: TaskRequest, manifest: ScenarioManifest | None
    ) -> ErrorCode | None:
        """Submit-time policy check: ``TASK_REJECTED`` (or another code) to refuse the task."""
        ...

    async def run(
        self,
        request: TaskRequest,
        *,
        provider: LLMProvider,
        manifest: ScenarioManifest,
        fixtures_root: Path,
        budget: TokenBudget,
        tracer: trace.Tracer | None = None,
    ) -> PipelineOutcome:
        """Run one task attempt. Never raises for expected failures; returns a FAILED outcome."""
        ...
