from __future__ import annotations

from pathlib import Path

from nexus_sigint.settings import SigintSettings


def test_sigint_identity() -> None:
    ok = SigintSettings(service_token="s" * 40, nexus_llm_mode="replay")
    assert ok.service_name == "sigint-agent" and ok.sqlite_path == Path("var/sigint.sqlite")
