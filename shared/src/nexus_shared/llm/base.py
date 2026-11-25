"""Provider abstraction (spec §13.2).

Signature vs spec §13.2: the spec lists ``task, prompt_version, input_data, output_schema,
budget``. Three keyword parameters are added so every provider can build the replay key and send
the same prompt:

- ``instructions``: the prompt file's text (system/developer instructions);
- ``prompt_sha256``: hash of that text (ADR-006 E2), part of the replay key;
- ``call_ordinal``: 0 = primary call, 1 = repair (§13.2);
- ``key_context``: scenario/agent/task type/schema version, the rest of the key material.

The user message every provider sends (and the replay key hashes) is
``canonical_json(normalize_input(input_data))``: what the model sees is exactly what is keyed.

``budget`` is read-only for providers (they cap ``max_output_tokens`` with it); the caller does
the pre-call estimate and the post-call charge (``TokenBudget``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from nexus_contracts.a2a.task import TaskUsage
from nexus_shared.llm.budget import TokenBudget
from nexus_shared.llm.keys import ReplayKeyContext, canonical_json, normalize_input


@dataclass(frozen=True, slots=True)
class LLMResult:
    """A schema-valid output plus accounting. ``usage.llm_calls`` is 1 per provider call."""

    output: BaseModel
    usage: TaskUsage
    model_id: str
    latency_ms: int


@runtime_checkable
class LLMProvider(Protocol):
    async def generate_structured(
        self,
        *,
        task: str,
        instructions: str,
        prompt_version: str,
        prompt_sha256: str,
        input_data: dict[str, Any],
        output_schema: type[BaseModel],
        budget: TokenBudget,
        call_ordinal: int,
        key_context: ReplayKeyContext,
    ) -> LLMResult:
        """Return a validated output, or raise ``LLMOutputInvalidError`` (repairable),
        ``LLMUnavailableError`` or ``ReplayMissError``."""
        ...


def render_user_message(input_data: Mapping[str, Any]) -> str:
    """The user message for ``input_data`` (identical for live, replay and estimation)."""
    return canonical_json(normalize_input(input_data))
