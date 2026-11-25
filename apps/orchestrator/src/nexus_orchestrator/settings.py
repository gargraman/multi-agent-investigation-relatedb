"""Orchestrator configuration (spec §13.3). Validated at startup; a bad value fails fast.

Env vars (no prefix, matching the spec's dotenv): ``SERVICE_TOKEN``, ``SQLITE_PATH``,
``CONTEXT_SERVICE_URL``, ``SCENARIOS_DIR``, ``NEXUS_MAX_CONCURRENT_INVESTIGATIONS``,
``NEXUS_MAX_TOKENS_PER_INVESTIGATION``, plus the timing knobs below (overridable so tests run fast).
"""

from pathlib import Path

from pydantic import Field

from nexus_shared.config import ServiceSettings


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
