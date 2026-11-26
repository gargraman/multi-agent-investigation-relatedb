"""Reusable LLM steps for agent pipelines.

:func:`classify_with_repair` is the S2 SIGINT loop (FR-028), extracted unchanged: one primary
structured call plus at most one repair call that is told the previous output's errors. Budget is
checked before each call and charged after it; span names and attributes are the S2 ones
(``gen_ai.generate`` with ``nexus.call_ordinal`` and the ``gen_ai.*`` model/usage attributes).

:func:`check_classifications` holds the post-parse rules every classify output shares (ids exactly
the given aliases, confidence in [0, 1], non-empty text within length caps, and the S3b output
guard of :func:`text_violations` on that text), and :func:`fence`
wraps untrusted fixture text for the model. Both moved here from SIGINT unchanged: SIGINT's S2
replay keys hash the fenced text, so its bytes must never drift.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

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

FENCE_OPEN = "<<<UNTRUSTED_EVIDENCE"
FENCE_CLOSE = "UNTRUSTED_EVIDENCE>>>"


def fence(evidence_id: str, text: str) -> str:
    """Wrap untrusted text in fence markers, neutralising any marker-like text inside it."""
    cleaned = text.replace("<<<", "< < <").replace(">>>", "> > >")
    cleaned = cleaned.replace("UNTRUSTED_EVIDENCE", "UNTRUSTED-TEXT")
    return f"{FENCE_OPEN} id={evidence_id}\n{cleaned}\n{FENCE_CLOSE}"


class Classified(Protocol):
    """One per-candidate classification item (any agent's ``CandidateClassification``)."""

    @property
    def candidate_id(self) -> str: ...

    @property
    def confidence(self) -> float: ...

    @property
    def statement(self) -> str: ...

    @property
    def reasoning_summary(self) -> str: ...


_FENCE_RE = re.compile(r"<<<|>>>|UNTRUSTED_EVIDENCE|UNTRUSTED-TEXT")
_URL_RE = re.compile(r"\bhttps?://|\bwww\.", re.IGNORECASE)
_TOKEN_RE = re.compile(r"NEXUS-")
_REF_RE = re.compile(
    r"\bLOC-GRID-\d{2}\b|\bENT-[A-Z0-9-]+\b|\bHDL-[A-Z0-9-]+\b|\bev-[a-z]+-\d{3}\b"
)


def text_violations(field: str, text: str, allowed_refs: frozenset[str]) -> list[str]:
    """Output-side guard on one model-written text field (S3b). ``field`` is the message prefix
    (e.g. ``C1.statement``). Returns at most one message per rule; messages never echo the
    offending text, because the repair prompt would re-inject it."""
    errors: list[str] = []
    if _FENCE_RE.search(text):
        errors.append(f"{field} contains fence markers")
    if _URL_RE.search(text):
        errors.append(f"{field} contains a URL")
    if _TOKEN_RE.search(text):
        errors.append(f"{field} contains a reserved token")
    if any(m.group(0) not in allowed_refs for m in _REF_RE.finditer(text)):
        errors.append(f"{field} references an id outside its candidate")
    return errors


def check_classifications(
    items: Sequence[Classified],
    candidate_ids: list[str],
    statement_max: int,
    reasoning_max: int,
    *,
    allowed_refs: Mapping[str, frozenset[str]],
) -> list[str]:
    """Post-parse rules. Returns content-free violation messages (empty list = valid).

    ``allowed_refs`` maps each candidate alias to the ids its text may cite (the candidate's
    ``about`` ids and evidence ids); a valid, length-capped text is also run through
    :func:`text_violations`."""
    errors: list[str] = []
    seen = [c.candidate_id for c in items]
    expected = set(candidate_ids)
    missing = sorted(expected - set(seen), key=_alias_order)
    unknown = sorted({s for s in seen if s not in expected}, key=_alias_order)
    dupes = sorted({s for s in seen if seen.count(s) > 1}, key=_alias_order)
    if missing:
        errors.append(f"missing candidate_id(s): {', '.join(missing)}")
    if unknown:
        errors.append(f"unknown candidate_id(s) (only {_range(candidate_ids)} exist)")
    if dupes:
        errors.append(f"duplicate candidate_id(s): {', '.join(dupes)}")
    for c in items:
        if c.candidate_id not in expected:
            continue
        where = c.candidate_id
        refs = allowed_refs.get(where, frozenset())
        if not math.isfinite(c.confidence) or not 0.0 <= c.confidence <= 1.0:
            errors.append(f"{where}.confidence must be within [0, 1]")
        if not c.statement.strip():
            errors.append(f"{where}.statement must not be empty")
        elif len(c.statement) > statement_max:
            errors.append(f"{where}.statement exceeds {statement_max} characters")
        else:
            errors.extend(text_violations(f"{where}.statement", c.statement, refs))
        if not c.reasoning_summary.strip():
            errors.append(f"{where}.reasoning_summary must not be empty")
        elif len(c.reasoning_summary) > reasoning_max:
            errors.append(f"{where}.reasoning_summary exceeds {reasoning_max} characters")
        else:
            errors.extend(text_violations(f"{where}.reasoning_summary", c.reasoning_summary, refs))
    return errors


def _alias_order(alias: str) -> tuple[int, str]:
    digits = alias[1:]
    return (int(digits), alias) if alias.startswith("C") and digits.isdigit() else (10**9, alias)


def _range(ids: list[str]) -> str:
    return f"{ids[0]}..{ids[-1]}" if ids else "none"


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
