"""Reusable LLM steps for agent pipelines.

:func:`classify_with_repair` is the S2 SIGINT loop (FR-028), extracted unchanged: one primary
structured call plus at most one repair call that is told the previous output's errors. Budget is
checked before each call and charged after it; span names and attributes are the S2 ones
(``gen_ai.generate`` with ``nexus.call_ordinal`` and the ``gen_ai.*`` model/usage attributes).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from pydantic import BaseModel

from nexus_contracts.a2a.task import TaskUsage
from nexus_contracts.api.errors import ErrorCode
from nexus_shared.agent.prompts import Prompt
from nexus_shared.llm import (
    BudgetExceededError,
    LLMOutputInvalidError,
    LLMProvider,
    LLMResult,
    LLMUnavailableError,
    ReplayKeyContext,
    ReplayMissError,
    TokenBudget,
    render_user_message,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClassifyResult:
    """``output`` is the validated model output when ``error`` is ``None``. ``usage`` and
    ``model_id`` cover every call made, including a failed one."""

    output: BaseModel | None
    usage: TaskUsage
    model_id: str | None
    error: ErrorCode | None = None


def add_usage(a: TaskUsage, b: TaskUsage) -> TaskUsage:
    return TaskUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        llm_calls=a.llm_calls + b.llm_calls,
        replayed=a.replayed or b.replayed,
    )


async def classify_with_repair(
    *,
    provider: LLMProvider,
    prompt: Prompt,
    input_data: dict[str, Any],
    output_schema: type[BaseModel],
    check: Callable[[BaseModel], list[str]],
    budget: TokenBudget,
    key_context: ReplayKeyContext,
    llm_task: str,
    tracer: trace.Tracer,
) -> ClassifyResult:
    """Primary call + one repair. Errors: ``BUDGET_EXCEEDED``, ``REPLAY_MISS``,
    ``LLM_UNAVAILABLE``, or ``SCHEMA_INVALID`` when neither call produced an output that parses
    and passes ``check`` (which returns a list of semantic errors, empty when the output is ok)."""
    usage = TaskUsage(input_tokens=0, output_tokens=0, llm_calls=0)
    model_id: str | None = None

    def failed(code: ErrorCode) -> ClassifyResult:
        return ClassifyResult(output=None, usage=usage, model_id=model_id, error=code)

    errors: list[str] = []
    for ordinal in (0, 1):  # primary + one repair (FR-028)
        data = input_data if ordinal == 0 else {**input_data, "repair": {"errors": errors}}
        estimate = budget.estimate_call(prompt.text, render_user_message(data))
        try:
            budget.ensure_can_spend(estimate)
        except BudgetExceededError:
            return failed(ErrorCode.BUDGET_EXCEEDED)
        with tracer.start_as_current_span("gen_ai.generate") as span:
            span.set_attribute("nexus.call_ordinal", ordinal)
            try:
                call: LLMResult = await provider.generate_structured(
                    task=llm_task,
                    instructions=prompt.text,
                    prompt_version=prompt.version,
                    prompt_sha256=prompt.sha256,
                    input_data=data,
                    output_schema=output_schema,
                    budget=budget,
                    call_ordinal=ordinal,
                    key_context=key_context,
                )
            except LLMOutputInvalidError as exc:
                usage = add_usage(usage, exc.usage)
                model_id = exc.model_id
                errors = [exc.reason]
                try:
                    budget.charge(exc.usage)
                except BudgetExceededError:
                    return failed(ErrorCode.BUDGET_EXCEEDED)
                continue
            except ReplayMissError as exc:
                log.error("replay miss", extra={"expected_key": exc.expected_key})
                return failed(ErrorCode.REPLAY_MISS)
            except LLMUnavailableError as exc:
                log.warning("llm unavailable", extra={"reason": str(exc)})
                return failed(ErrorCode.LLM_UNAVAILABLE)
            usage = add_usage(usage, call.usage)
            model_id = call.model_id
            span.set_attribute("gen_ai.request.model", call.model_id)
            span.set_attribute("gen_ai.usage.input_tokens", call.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", call.usage.output_tokens)
        try:
            budget.charge(call.usage)
        except BudgetExceededError:
            return failed(ErrorCode.BUDGET_EXCEEDED)
        assert isinstance(call.output, output_schema)
        errors = check(call.output)
        if not errors:
            return ClassifyResult(output=call.output, usage=usage, model_id=model_id)
    return failed(ErrorCode.SCHEMA_INVALID)
