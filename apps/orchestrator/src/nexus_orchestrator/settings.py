"""Orchestrator configuration (spec §13.3). Validated at startup; a bad value fails fast.

Env vars (no prefix, matching the spec's dotenv): ``SERVICE_TOKEN``, ``SQLITE_PATH``,
``CONTEXT_SERVICE_URL``, ``SCENARIOS_DIR``, ``NEXUS_MAX_CONCURRENT_INVESTIGATIONS``,
``NEXUS_MAX_TOKENS_PER_INVESTIGATION``, plus the timing knobs below (overridable so tests run fast).

S2 adds the agent registry (S2 spec FR-001, G14):

- ``NEXUS_ENABLED_AGENTS``: comma list of agent ids that get task rows (default ``sigint-agent``).
  Template agents not listed are *skipped* (no task row, listed in ``skipped_agents``; G1).
- ``NEXUS_AGENT_URLS``: JSON object ``{agent_id: base_url}``. Unknown agent ids fail startup, and
  every enabled agent needs a URL.
"""

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import NoDecode

from nexus_contracts.common import AgentId
from nexus_shared.config import ServiceSettings

# S2 has no ANALYZING phase: the analysis agent is dispatched only after the specialists (§8.1
# step 11), which arrives in S4. Enabling it now would dispatch it alongside SIGINT.
NOT_YET_DISPATCHABLE = frozenset({AgentId.ANALYSIS})


class OrchestratorSettings(ServiceSettings):
    service_name: str = "orchestrator"

    sqlite_path: Path = Path("var/orchestrator.sqlite")
    context_service_url: str = "http://context-service:8000"
    scenarios_dir: Path = Path("data/scenarios")

    # Spec §13.3. Counts investigations in a non-terminal status (CREATED/RUNNING/ANALYZING).
    nexus_max_concurrent_investigations: int = Field(default=2, ge=1)
    # Spec §13.3 / §8.5: stored as investigations.token_budget at create.
    nexus_max_tokens_per_investigation: int = Field(default=60000, gt=0)
    # Spec §8.5: investigation wall-clock deadline (10 min).
    investigation_deadline_seconds: float = Field(default=600, gt=0)

    # Outbound HTTP.
    context_service_timeout_seconds: float = Field(default=5.0, gt=0)
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0)
    # Background retry of failed Context Service creates (ADR-002 A1).
    context_retry_interval_seconds: float = Field(default=5.0, gt=0)

    # Leader lock (ADR-005 D1, ADR-008 F3).
    leader_heartbeat_seconds: float = Field(default=5.0, gt=0)
    leader_stale_seconds: float = Field(default=15.0, gt=0)
    leader_wait_seconds: float = Field(default=30.0, ge=0)
    leader_poll_seconds: float = Field(default=1.0, gt=0)

    # SSE heartbeat comment interval.
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)

    # -- S2: agents (FR-001..FR-008) -------------------------------------------------------------
    nexus_enabled_agents: Annotated[tuple[AgentId, ...], NoDecode] = (AgentId.SIGINT,)
    nexus_agent_urls: dict[AgentId, str] = Field(
        default_factory=lambda: {AgentId.SIGINT: "http://sigint-agent:8000"}
    )
    # Spec §8.5: specialist task deadline (180 s), capped by the investigation deadline.
    task_deadline_seconds: float = Field(default=180, gt=0)
    # FR-003: POST /v1/tasks; connect error / timeout / 5xx retried this often at this backoff.
    dispatch_timeout_seconds: float = Field(default=5.0, gt=0)
    dispatch_max_retries: int = Field(default=3, ge=0)
    dispatch_backoff_seconds: float = Field(default=2.0, ge=0)
    # FR-004 / §8.3: poll every 2 s ± 20 %, 2 s connect / 5 s read.
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    poll_jitter_ratio: float = Field(default=0.2, ge=0, lt=1)
    poll_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    poll_read_timeout_seconds: float = Field(default=5.0, gt=0)
    # FR-008: agent cards are read at startup and every 30 s (E4 objective lock, E8 health).
    agent_card_refresh_seconds: float = Field(default=30.0, gt=0)

    @field_validator("nexus_enabled_agents", mode="before")
    @classmethod
    def _split_agents(cls, value: Any) -> Any:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def _registry_consistent(self) -> "OrchestratorSettings":
        if len(set(self.nexus_enabled_agents)) != len(self.nexus_enabled_agents):
            raise ValueError("NEXUS_ENABLED_AGENTS contains duplicates")
        blocked = NOT_YET_DISPATCHABLE & set(self.nexus_enabled_agents)
        if blocked:
            raise ValueError(
                f"NEXUS_ENABLED_AGENTS: {sorted(blocked)} cannot be enabled yet "
                "(the analysis phase arrives in S4)"
            )
        missing = [a.value for a in self.nexus_enabled_agents if a not in self.nexus_agent_urls]
        if missing:
            raise ValueError(f"NEXUS_AGENT_URLS has no URL for enabled agent(s) {missing}")
        for agent, url in self.nexus_agent_urls.items():
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"NEXUS_AGENT_URLS[{agent.value}] must be an http(s) URL")
        return self

    def agent_url(self, agent: AgentId) -> str:
        return self.nexus_agent_urls[agent].rstrip("/")
