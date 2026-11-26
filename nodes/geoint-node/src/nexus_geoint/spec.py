"""GEOINT as an :class:`~nexus_shared.agent.spec.AgentSpec` plug-in for the shared agent runtime.

Identity, task types and agent-card text live here; the submit-time policy check and the pure
pipeline are :func:`nexus_geoint.pipeline.validate_request` and :func:`nexus_geoint.pipeline.run`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace

from nexus_contracts.a2a.task import TaskRequest
from nexus_contracts.api.errors import ErrorCode
from nexus_contracts.common import AgentId
from nexus_contracts.scenario.manifest import ScenarioManifest
from nexus_geoint import __version__, pipeline
from nexus_shared.agent.spec import AgentSpec, PipelineOutcome
from nexus_shared.llm import LLMProvider, TokenBudget


@dataclass(frozen=True)
class GeointSpec:
    agent_id: AgentId = pipeline.AGENT_ID
    task_types: tuple[str, ...] = pipeline.TASK_TYPES
    role: str = "GEOINT specialist: co-located and conflicting placements in synthetic sightings"
    version: str = __version__

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


GEOINT: AgentSpec = GeointSpec()
