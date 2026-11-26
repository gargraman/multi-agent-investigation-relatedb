"""Context Service configuration (spec §13.3, ADR-001 R4).

Fails fast at startup on: a missing store path, a missing/short service or caller token, or a
``RELATE_DURABILITY_MODE`` other than ``full`` (``everysecond``/``snapshot`` silently accept data
loss, ADR-001 R4).
"""

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, field_validator

from nexus_shared.config import ServiceSettings
from nexus_shared.security import ORCHESTRATOR, TokenRegistry, token_env_var

DURABILITY_ENV = "RELATE_DURABILITY_MODE"

# Callers whose bearer tokens this service requires at startup (S2 FR-045; geoint-agent from S3).
ACCEPTED_CALLERS: tuple[str, ...] = (ORCHESTRATOR, "sigint-agent", "geoint-agent")
# Callers whose tokens are accepted when configured, but not required.
# TODO(S4): analysis-agent (search/assessments/context with clearance filtering, spec §9.5).
OPTIONAL_CALLERS: tuple[str, ...] = ()


class DurabilityModeError(RuntimeError):
    """``RELATE_DURABILITY_MODE`` is set to something other than ``full``."""


def assert_durability_full(env: Mapping[str, str] | None = None) -> None:
    """Raise unless ``RELATE_DURABILITY_MODE`` is unset or ``full`` (ADR-001 R4).

    Relate reads the variable from the process environment itself, so this checks
    ``os.environ`` directly as well as through settings validation."""
    env = os.environ if env is None else env
    mode = env.get(DURABILITY_ENV)
    if mode is not None and mode != "full":
        raise DurabilityModeError(f"{DURABILITY_ENV}={mode!r} is refused; only 'full' is allowed")


class ContextServiceSettings(ServiceSettings):
    service_name: str = "context-service"

    relate_store_path: Path = Field(description="Relate store directory (named volume).")
    relate_flush_interval_s: float = Field(
        default=30.0, gt=0, description="Seconds between lane checkpoints (ADR-001 R2)."
    )
    relate_durability_mode: str | None = Field(
        default=None, description="Mirrors RELATE_DURABILITY_MODE; only unset or 'full'."
    )

    @field_validator("relate_durability_mode")
    @classmethod
    def _durability_full(cls, v: str | None) -> str | None:
        if v is not None and v != "full":
            raise ValueError(f"{DURABILITY_ENV}={v!r} is refused; only 'full' is allowed")
        return v


def load_token_registry(env: Mapping[str, str] | None = None) -> TokenRegistry:
    """Tokens of every accepted caller; a missing/short required token raises (startup error).

    An optional caller's token is loaded when set; set-but-short is still a startup error."""
    env = os.environ if env is None else env
    required = TokenRegistry.from_env(ACCEPTED_CALLERS, env)
    present = [c for c in OPTIONAL_CALLERS if env.get(token_env_var(c))]
    optional = TokenRegistry.from_env(present, env)  # raises on a short token
    return TokenRegistry({**required.tokens, **optional.tokens})
