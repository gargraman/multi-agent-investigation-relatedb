"""SIGINT agent configuration (spec §13.3, S2 §10.3). Validated at startup; bad config fails fast.

Env vars (no prefix): ``SERVICE_TOKEN`` (this agent's own token, sent to the Context Service),
``NEXUS_TOKEN_ORCHESTRATOR`` (the only token accepted on ``/v1/tasks``; read by
``load_token_registry``), ``CONTEXT_SERVICE_URL``, ``SQLITE_PATH``, ``SCENARIOS_DIR``,
``FIXTURES_DIR``, ``NEXUS_LLM_MODE`` (live|replay, no runtime switch), ``REPLAY_DIR``,
``OPENAI_API_KEY`` (required in live mode), ``OPENAI_MODEL_EXTRACT``, ``OPENAI_TIMEOUT_SECONDS``,
``OPENAI_MAX_RETRIES``, ``OPENAI_REASONING_EFFORT`` (optional), ``EXTRA_CA_FILE`` (optional).

Paths:

- ``FIXTURES_DIR`` is the root that manifest ``file:`` paths are relative to, i.e. the directory
  that *contains* ``sigint/`` (``data/fixtures`` locally, ``/app/data/fixtures`` in compose where
  only ``fixtures/sigint`` is mounted under it).
- ``REPLAY_DIR`` is the replay root that *contains* ``sigint-agent/``; recordings are read from
  ``REPLAY_DIR/sigint-agent/<scenario_id>/<key>.json`` (``/app/data/replay`` in compose).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, SecretStr, model_validator

from nexus_contracts.common import LlmMode
from nexus_shared.config import ServiceSettings
from nexus_shared.security import ORCHESTRATOR, TokenRegistry

DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
ACCEPTED_CALLERS: tuple[str, ...] = (ORCHESTRATOR,)


class SigintSettings(ServiceSettings):
    service_name: str = "sigint-agent"

    context_service_url: str = "http://context-service:8000"
    sqlite_path: Path = Path("var/sigint.sqlite")
    scenarios_dir: Path = Path("data/scenarios")
    fixtures_dir: Path = Path("data/fixtures")

    nexus_llm_mode: LlmMode = LlmMode.LIVE
    replay_dir: Path = Path("data/replay")
    openai_api_key: SecretStr | None = None
    openai_model_extract: str = DEFAULT_MODEL
    openai_timeout_seconds: float = Field(default=45.0, gt=0)
    openai_max_retries: int = Field(default=2, ge=0)
    openai_reasoning_effort: str | None = None
    extra_ca_file: Path | None = None

    # Context Service submit (FR-032): 1 try + 2 retries on connect error / 5xx.
    context_timeout_seconds: float = Field(default=10.0, gt=0)
    context_retries: int = Field(default=2, ge=0)
    context_backoff_seconds: float = Field(default=1.0, ge=0)
    # Agent-side deadline self-cancel grace (spec §8.3, ADR-003 B3).
    deadline_grace_seconds: float = Field(default=5.0, ge=0)

    @model_validator(mode="after")
    def _live_needs_key(self) -> SigintSettings:
        if self.nexus_llm_mode is LlmMode.LIVE and (
            self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip()
        ):
            raise ValueError("OPENAI_API_KEY is required when NEXUS_LLM_MODE=live")
        return self


def load_token_registry(env: Mapping[str, str] | None = None) -> TokenRegistry:
    """Only the orchestrator may call ``/v1/tasks`` (S2 §10.1)."""
    return TokenRegistry.from_env(ACCEPTED_CALLERS, env)
