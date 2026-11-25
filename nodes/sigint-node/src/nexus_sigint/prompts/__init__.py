"""Versioned prompts, shipped inside the package and loaded via ``importlib.resources``
(S2 spec G11), so they are present in non-editable installs (container images) too."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from nexus_shared.llm.keys import prompt_sha256

CLASSIFY_PROMPT = "sigint-classify@1.0.0"


@dataclass(frozen=True, slots=True)
class Prompt:
    version: str  # "<name>@<semver>"
    text: str
    sha256: str

    @property
    def name(self) -> str:
        return self.version.split("@", 1)[0]


@cache
def load_prompt(version: str = CLASSIFY_PROMPT) -> Prompt:
    text = files(__package__).joinpath(f"{version}.md").read_text(encoding="utf-8")
    return Prompt(version=version, text=text, sha256=prompt_sha256(text))
