"""GEOINT agent configuration (spec §13.3, S2 §10.3).

The fields, env vars and path semantics live in :mod:`nexus_shared.agent.settings`; this module
only pins the GEOINT identity (``service_name``, ``sqlite_path``). ``REPLAY_DIR`` therefore holds
``geoint-agent/``: recordings are read from ``REPLAY_DIR/geoint-agent/<scenario_id>/<key>.json``.
"""

from __future__ import annotations

from pathlib import Path

from nexus_shared.agent.settings import (
    ACCEPTED_CALLERS,
    DEFAULT_MODEL,
    AgentSettings,
    load_token_registry,
)

__all__ = ["ACCEPTED_CALLERS", "DEFAULT_MODEL", "GeointSettings", "load_token_registry"]


class GeointSettings(AgentSettings):
    service_name: str = "geoint-agent"
    sqlite_path: Path = Path("var/geoint.sqlite")
