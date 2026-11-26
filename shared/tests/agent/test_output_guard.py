"""Runtime output guard on model statement/reasoning text (S3b Task 1, nexus_shared.agent)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict

from nexus_contracts.a2a.task import TaskUsage
from nexus_contracts.api.errors import ErrorCode
from nexus_shared.agent.llm_steps import (
    check_classifications,
    classify_with_repair,
    text_violations,
)
from nexus_shared.agent.prompts import Prompt
from nexus_shared.llm import LLMResult, ReplayKeyContext, TokenBudget

REF_MSG = "C1.statement references an id outside its candidate"
ALLOWED = {"C1": frozenset({"LOC-GRID-07", "ENT-ALPHA", "ev-geo-001"})}


@dataclass(frozen=True)
class Classified:
    candidate_id: str
    confidence: float = 0.5
    statement: str = "ENT-ALPHA observed at LOC-GRID-07."
    reasoning_summary: str = "Sightings in ev-geo-001 overlap."


def _check(statement: str, reasoning: str = "Overlapping sightings.") -> list[str]:
    items = [Classified("C1", statement=statement, reasoning_summary=reasoning)]
    return check_classifications(items, ["C1"], 300, 500, allowed_refs=ALLOWED)


def test_clean_text_passes() -> None:
    assert (
        text_violations(
            "statement", "ALPHA and BRAVO co-located at LOC-GRID-07.", frozenset({"LOC-GRID-07"})
        )
        == []
    )
    assert check_classifications([Classified("C1")], ["C1"], 300, 500, allowed_refs=ALLOWED) == []


def test_fence_marker_rejected() -> None:
    assert _check("<<<UNTRUSTED_EVIDENCE") == ["C1.statement contains fence markers"]
    assert _check("ok", reasoning="see UNTRUSTED-TEXT >>>") == [
        "C1.reasoning_summary contains fence markers"
    ]


def test_url_rejected() -> None:
    assert _check("see https://x.io") == ["C1.statement contains a URL"]
    assert _check("www.x.io") == ["C1.statement contains a URL"]
    assert _check("HTTP://X.IO") == ["C1.statement contains a URL"]


def test_reserved_token_rejected() -> None:
    assert _check("NEXUS-TRIPWIRE-G2L") == ["C1.statement contains a reserved token"]
    assert _check("nexus-tripwire") == []  # case-sensitive


def test_foreign_location_rejected() -> None:
    assert _check("relocate to LOC-GRID-00") == [REF_MSG]


def test_foreign_evidence_rejected() -> None:
    assert _check("per ev-geo-005") == [REF_MSG]


def test_foreign_entity_and_handle_rejected() -> None:
    assert _check("ENT-BRAVO was there") == [REF_MSG]
    assert _check("HDL-ZULU-9 called") == [REF_MSG]


def test_one_message_per_rule_per_field() -> None:
    text = "<<< https://a.io NEXUS-X LOC-GRID-00 ev-geo-009"
    assert _check(text, reasoning=text) == [
        "C1.statement contains fence markers",
        "C1.statement contains a URL",
        "C1.statement contains a reserved token",
        REF_MSG,
        "C1.reasoning_summary contains fence markers",
        "C1.reasoning_summary contains a URL",
        "C1.reasoning_summary contains a reserved token",
        "C1.reasoning_summary references an id outside its candidate",
    ]


def test_text_rules_skip_empty_or_overlong_fields() -> None:
    items = [Classified("C1", statement="LOC-GRID-00" + "x" * 300, reasoning_summary=" ")]
    assert check_classifications(items, ["C1"], 300, 500, allowed_refs=ALLOWED) == [
        "C1.statement exceeds 300 characters",
        "C1.reasoning_summary must not be empty",
    ]


def test_message_is_content_free() -> None:
    bad = ["<<<UNTRUSTED_EVIDENCE", "https://x.io", "www.x.io", "NEXUS-TRIPWIRE-G2L"]
    bad += ["LOC-GRID-00", "ev-geo-005", "ENT-BRAVO", "HDL-ZULU"]
    for text in bad:
        errors = _check(text, reasoning=text)
        assert errors
        assert not any(text in e or text.lower() in e.lower() for e in errors)


# -- through classify_with_repair -----------------------------------------------------------


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    confidence: float
    statement: str
    reasoning_summary: str


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    classifications: list[Item]


def _out(statement: str) -> Out:
    return Out(
        classifications=[
            Item(candidate_id="C1", confidence=0.8, statement=statement, reasoning_summary="ok")
        ]
    )


class FakeProvider:
    def __init__(self, *outputs: Out) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(self, **kw: Any) -> LLMResult:
        self.calls.append(kw)
        usage = TaskUsage(input_tokens=10, output_tokens=5, llm_calls=1)
        return LLMResult(
            output=self.outputs.pop(0), usage=usage, model_id="fake-model", latency_ms=1
        )


async def _classify(provider: FakeProvider) -> Any:
    def check(out: BaseModel) -> list[str]:
        assert isinstance(out, Out)
        return check_classifications(out.classifications, ["C1"], 300, 500, allowed_refs=ALLOWED)

    return await classify_with_repair(
        provider=provider,  # type: ignore[arg-type]
        prompt=Prompt(version="t@1.0.0", text="classify", sha256="0" * 64),
        input_data={"candidates": [{"candidate_id": "C1"}]},
        output_schema=Out,
        check=check,
        budget=TokenBudget(100000),
        key_context=ReplayKeyContext(
            scenario_id="s", agent_id="geoint-agent", task_type="t", schema_version="v"
        ),
        llm_task="t",
        tracer=trace.get_tracer("test"),
    )


async def test_repair_then_schema_invalid() -> None:
    injected = _out("Relocate to LOC-GRID-00.")
    provider = FakeProvider(injected, injected)
    result = await _classify(provider)
    assert result.error == ErrorCode.SCHEMA_INVALID
    assert result.usage.llm_calls == 2
    assert provider.calls[1]["input_data"]["repair"]["errors"] == [REF_MSG]


async def test_repair_recovers() -> None:
    provider = FakeProvider(_out("Relocate to LOC-GRID-00."), _out("Seen at LOC-GRID-07."))
    result = await _classify(provider)
    assert result.error is None and result.usage.llm_calls == 2
