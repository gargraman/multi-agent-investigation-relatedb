from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from nexus_shared.agent.settings import AgentSettings

BASE = {"service_name": "test-agent", "service_token": "s" * 40, "sqlite_path": "var/t.sqlite"}


def test_live_mode_requires_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        AgentSettings(**BASE, nexus_llm_mode="live")
    ok = AgentSettings(**BASE, nexus_llm_mode="replay")
    assert ok.fixtures_dir == Path("data/fixtures") and ok.replay_dir == Path("data/replay")


def test_sqlite_path_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SQLITE_PATH", raising=False)
    body = {k: v for k, v in BASE.items() if k != "sqlite_path"}
    with pytest.raises(ValidationError, match="sqlite_path"):
        AgentSettings(**body, nexus_llm_mode="replay")
