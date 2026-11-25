from __future__ import annotations

from pathlib import Path

import pytest

from nexus_sigint.settings import SigintSettings


def test_sigint_identity_and_live_mode_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        SigintSettings(service_token="s" * 40, nexus_llm_mode="live")
    ok = SigintSettings(service_token="s" * 40, nexus_llm_mode="replay")
    assert ok.service_name == "sigint-agent" and ok.sqlite_path == Path("var/sigint.sqlite")
