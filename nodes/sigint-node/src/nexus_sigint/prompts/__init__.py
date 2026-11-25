"""SIGINT prompts, shipped as package data next to this module (S2 spec G11). Loading and
hashing live in :mod:`nexus_shared.agent.prompts`."""

from __future__ import annotations

from nexus_shared.agent.prompts import Prompt
from nexus_shared.agent.prompts import load_prompt as _load_prompt

__all__ = ["CLASSIFY_PROMPT", "Prompt", "load_prompt"]

CLASSIFY_PROMPT = "sigint-classify@1.0.0"


def load_prompt(version: str = CLASSIFY_PROMPT) -> Prompt:
    return _load_prompt(__package__, version)
