"""Process-wide objects, built in ``create_app`` and opened/closed in the lifespan."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from opentelemetry.trace import Tracer

from nexus_orchestrator.delegation.agent_client import AgentClient
from nexus_orchestrator.delegation.agent_registry import AgentRegistry
from nexus_orchestrator.delegation.context_client import ContextServiceClient
from nexus_orchestrator.pubsub import TimelineBroker
from nexus_orchestrator.scenarios import ScenarioCatalog
from nexus_orchestrator.settings import OrchestratorSettings
from nexus_orchestrator.state.db import Database
from nexus_orchestrator.state.leader import LeaderLock
from nexus_orchestrator.state.repository import Repository
from nexus_orchestrator.workflow.creation import InvestigationWorkflow
from nexus_orchestrator.workflow.fence import Fence
from nexus_orchestrator.workflow.poller import Poller


@dataclass
class Runtime:
    settings: OrchestratorSettings
    db: Database
    broker: TimelineBroker
    repo: Repository
    catalog: ScenarioCatalog
    context: ContextServiceClient
    leader: LeaderLock
    workflow: InvestigationWorkflow
    agents: AgentClient
    registry: AgentRegistry
    poller: Poller
    fence: Fence
    tracer: Tracer
    sweep_done: bool = False
    shutting_down: bool = False

    @property
    def ready(self) -> bool:
        return self.sweep_done and self.leader.held and self.db.is_open and not self.shutting_down


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime
