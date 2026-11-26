"""SIGINT as an :class:`~nexus_shared.agent.spec.AgentSpec` plug-in for the shared agent runtime.

Identity, task types and agent-card text live here; the submit-time policy check and the pure
pipeline are :func:`nexus_sigint.pipeline.validate_request` and :func:`nexus_sigint.pipeline.run`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace

from nexus_contracts.a2a.task import TaskRequest
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.common import AgentId, Classification
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_shared.agent.spec import AgentSpec, PipelineOutcome
from nexus_shared.llm import LLMProvider, TokenBudget
from nexus_sigint import __version__, pipeline


@dataclass(frozen=True)
class SigintSpec:
    agent_id: AgentId = pipeline.AGENT_ID
    task_types: tuple[str, ...] = pipeline.TASK_TYPES
    role: str = "SIGINT specialist: recurring contact patterns in synthetic communications metadata"
    version: str = __version__
    clearance: Classification = Classification.DEMO_HIGH  # = Context Service CLEARANCE

    def validate_request(
        self, request: TaskRequest, manifest: ScenarioManifest | None
    ) -> ErrorCode | None:
        return pipeline.validate_request(request, manifest)

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
        return await pipeline.run(
            request,
            provider=provider,
            manifest=manifest,
            fixtures_root=fixtures_root,
            budget=budget,
            tracer=tracer,
        )


SIGINT: AgentSpec = SigintSpec()
