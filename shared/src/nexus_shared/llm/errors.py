"""LLM-layer errors. Messages carry codes and counts only, never prompt or output text."""

from __future__ import annotations

from typing import Any

from nexus_contracts.a2a.task import TaskUsage


class LLMError(Exception):
    """Base class for provider failures."""


class LLMUnavailableError(LLMError):
    """The model could not be reached or refused the request (timeout, 5xx/429 after the SDK's
    retries, other 4xx). Task code ``llm_unavailable``."""


class LLMOutputInvalidError(LLMError):
    """The call returned, but the output is unusable: refusal, incomplete, not JSON, or not valid
    against the output schema. The caller may repair once (spec §13.4).

    ``usage`` is the call's real usage (it still counts against the budget). ``raw_response`` is
    the model output (parsed JSON when possible, else the text) so a recording can replay the
    same failure. It is never logged."""

    def __init__(
        self,
        reason: str,
        *,
        usage: TaskUsage,
        model_id: str,
        raw_response: Any = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.usage = usage
        self.model_id = model_id
        self.raw_response = raw_response


class ReplayMissError(LLMError):
    """No recording for this key (ADR-006). Task code ``replay_miss``. Nothing is fabricated.

    ``key_material`` is the full material, so the miss can be diagnosed by diffing it against the
    stored ``key_material`` of the nearest recording."""

    def __init__(self, expected_key: str, key_material: dict[str, Any]) -> None:
        super().__init__(f"no replay recording for key {expected_key}")
        self.expected_key = expected_key
        self.key_material = key_material
