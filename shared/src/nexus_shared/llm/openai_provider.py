"""Live provider: OpenAI Responses API with strict JSON Schema output (spec §13.2, ADR-006).

SDK facts verified against the installed ``openai`` package source (3.59.x):

- ``AsyncOpenAI(api_key=..., timeout=..., max_retries=..., http_client=...)``; ``http_client``
  must be an ``httpx.AsyncClient`` or ``httpx2.AsyncClient``. We build it with the public
  ``openai.DefaultAsyncHttpx2Client(verify=<SSLContext>, timeout=...)``.
- ``client.responses.create(model=, instructions=, input=, text={"format": {"type":
  "json_schema", "name": ..., "schema": ..., "strict": True}}, max_output_tokens=, store=,
  reasoning=)`` returns ``Response``.
- ``Response.status`` in {completed, failed, in_progress, cancelled, queued, incomplete};
  ``Response.incomplete_details.reason``; refusals are ``output[].content[]`` items with
  ``type == "refusal"``; ``Response.output_text`` joins ``output_text`` items;
  ``Response.usage.input_tokens`` / ``output_tokens`` (``output_tokens_details.reasoning_tokens``
  are included in ``output_tokens``); ``Response.model`` is the served model id.
- Errors: ``APITimeoutError``/``APIConnectionError`` (network), ``APIStatusError`` subclasses
  with ``status_code``. The SDK itself retries 408/409/429/5xx and connection errors
  ``max_retries`` times, so this provider adds no retry layer (§8.5 one layer per hop).

Nothing here logs prompt text, output text or the key.
"""

from __future__ import annotations

import logging
import ssl
import time
from typing import Any

import openai
from pydantic import BaseModel, SecretStr

from nexus_contracts.a2a.task import TaskUsage
from nexus_shared.llm.base import LLMResult, render_user_message
from nexus_shared.llm.budget import TokenBudget
from nexus_shared.llm.errors import LLMOutputInvalidError, LLMUnavailableError
from nexus_shared.llm.keys import ReplayKeyContext
from nexus_shared.llm.schema import OutputParseError, parse_output, strict_json_schema

log = logging.getLogger(__name__)

DEFAULT_MAX_OUTPUT_TOKENS = 8000


class OpenAIProvider:
    def __init__(
        self,
        model: str,
        api_key: SecretStr,
        timeout_s: float = 45.0,
        max_retries: int = 2,
        ssl_context: ssl.SSLContext | None = None,
        *,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        reasoning_effort: str | None = None,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        if not api_key.get_secret_value():
            raise ValueError("OPENAI_API_KEY is required in live mode")
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        if client is None:
            http_client = openai.DefaultAsyncHttpx2Client(
                verify=ssl_context if ssl_context is not None else True, timeout=timeout_s
            )
            client = openai.AsyncOpenAI(
                api_key=api_key.get_secret_value(),
                timeout=timeout_s,
                max_retries=max_retries,
                http_client=http_client,
            )
        self._client = client

    async def aclose(self) -> None:
        await self._client.close()

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
        user = render_user_message(input_data)
        input_estimate = budget.estimate_tokens(instructions) + budget.estimate_tokens(user)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": user,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": _format_name(task),
                    "schema": strict_json_schema(output_schema),
                    "strict": True,
                }
            },
            "max_output_tokens": budget.output_allowance(input_estimate, self.max_output_tokens),
            "store": False,
        }
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        started = time.monotonic()
        try:
            response = await self._client.responses.create(**kwargs)
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            raise LLMUnavailableError(f"openai unreachable: {type(exc).__name__}") from None
        except openai.APIStatusError as exc:
            raise LLMUnavailableError(f"openai status {exc.status_code}") from None
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._to_result(response, output_schema, latency_ms, call_ordinal)

    def _to_result(
        self, response: Any, output_schema: type[BaseModel], latency_ms: int, call_ordinal: int
    ) -> LLMResult:
        u = response.usage
        usage = TaskUsage(
            input_tokens=u.input_tokens if u else 0,
            output_tokens=u.output_tokens if u else 0,
            llm_calls=1,
        )
        model_id = str(response.model or self.model)
        log.info(
            "llm call done",
            extra={
                "model": model_id,
                "status": response.status,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "latency_ms": latency_ms,
                "call_ordinal": call_ordinal,
            },
        )
        text = response.output_text
        if _has_refusal(response):
            raise LLMOutputInvalidError(
                "model refused", usage=usage, model_id=model_id, raw_response=None
            )
        if response.status != "completed":
            reason = getattr(response.incomplete_details, "reason", None)
            raise LLMOutputInvalidError(
                f"response status {response.status} ({reason})",
                usage=usage,
                model_id=model_id,
                raw_response=text or None,
            )
        try:
            output = parse_output(output_schema, text)
        except OutputParseError as exc:
            raise LLMOutputInvalidError(
                exc.reason, usage=usage, model_id=model_id, raw_response=exc.raw
            ) from None
        return LLMResult(output=output, usage=usage, model_id=model_id, latency_ms=latency_ms)


def _format_name(task: str) -> str:
    """``text.format.name``: a-z, A-Z, 0-9, ``_`` or ``-``, at most 64 chars."""
    cleaned = "".join(c if c.isalnum() or c in "_-" else "_" for c in task)
    return cleaned[:64] or "output"


def _has_refusal(response: Any) -> bool:
    for item in response.output or []:
        if getattr(item, "type", None) == "message":
            for content in getattr(item, "content", None) or []:
                if getattr(content, "type", None) == "refusal":
                    return True
    return False
