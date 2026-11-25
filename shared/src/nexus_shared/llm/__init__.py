"""LLM provider layer (spec §13, ADR-006). Requires the ``nexus-shared[llm]`` extra for
``OpenAIProvider`` and ``build_ssl_context`` (``openai``, ``certifi``); both are imported lazily so
the rest of this package works without the extra."""

from typing import TYPE_CHECKING, Any

from nexus_shared.llm.base import LLMProvider, LLMResult, render_user_message
from nexus_shared.llm.budget import BudgetExceededError, TokenBudget
from nexus_shared.llm.errors import (
    LLMError,
    LLMOutputInvalidError,
    LLMUnavailableError,
    ReplayMissError,
)
from nexus_shared.llm.keys import (
    ReplayKeyContext,
    build_key_material,
    canonical_json,
    normalize_input,
    prompt_sha256,
    replay_key,
)
from nexus_shared.llm.replay import (
    API_KEY_PATTERN,
    REPLAY_MODEL_PREFIX,
    Recording,
    RecordingProvider,
    ReplayLLMProvider,
    recording_path,
)
from nexus_shared.llm.schema import strict_json_schema

if TYPE_CHECKING:
    from nexus_shared.llm.http import build_ssl_context
    from nexus_shared.llm.openai_provider import OpenAIProvider


def __getattr__(name: str) -> Any:
    if name == "OpenAIProvider":
        from nexus_shared.llm.openai_provider import OpenAIProvider

        return OpenAIProvider
    if name == "build_ssl_context":
        from nexus_shared.llm.http import build_ssl_context

        return build_ssl_context
    raise AttributeError(name)


__all__ = [
    "API_KEY_PATTERN",
    "REPLAY_MODEL_PREFIX",
    "BudgetExceededError",
    "LLMError",
    "LLMOutputInvalidError",
    "LLMProvider",
    "LLMResult",
    "LLMUnavailableError",
    "OpenAIProvider",
    "Recording",
    "RecordingProvider",
    "ReplayKeyContext",
    "ReplayLLMProvider",
    "ReplayMissError",
    "TokenBudget",
    "build_key_material",
    "build_ssl_context",
    "canonical_json",
    "normalize_input",
    "prompt_sha256",
    "recording_path",
    "render_user_message",
    "replay_key",
    "strict_json_schema",
]
