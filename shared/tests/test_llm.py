from __future__ import annotations

import json
import ssl
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from nexus_contracts.a2a.task import TaskUsage
from nexus_shared.llm import (
    API_KEY_PATTERN,
    BudgetExceededError,
    LLMOutputInvalidError,
    LLMResult,
    LLMUnavailableError,
    RecordingProvider,
    ReplayKeyContext,
    ReplayLLMProvider,
    ReplayMissError,
    TokenBudget,
    build_key_material,
    build_ssl_context,
    replay_key,
    strict_json_schema,
)
from nexus_shared.llm.schema import OutputParseError, parse_output

CTX = ReplayKeyContext("baseline-corroboration", "sigint-agent", "classify", "out@1.0.0")


class Out(BaseModel):
    label: str
    score: float
    items: list[Inner]


class Inner(BaseModel):
    title: str  # a field literally named "title" must survive schema cleaning


Out.model_rebuild()

VALID = {"label": "x", "score": 0.5, "items": [{"title": "t"}]}


def _key(input_data: dict, **kw: Any) -> str:
    args = {"prompt_version": "p@1", "prompt_sha256": "a" * 64, "call_ordinal": 0} | kw
    return replay_key(build_key_material(CTX, input_data=input_data, **args))


# --- replay key -------------------------------------------------------------------------------


def test_key_ignores_field_order_volatile_and_generated_ids() -> None:
    a = {"x": 1, "y": [1.0000001, "b"], "task_id": "tsk_1", "deadline": "now", "ref": "inv_abc"}
    b = {"ref": "inv_zzz", "y": [1.0, "b"], "x": 1, "task_id": "tsk_2", "attempt": 3}
    assert _key(a) == _key(b)


def test_key_normalises_unicode_nfc() -> None:
    assert _key({"s": "café"}) == _key({"s": "café"})


@pytest.mark.parametrize(
    "change",
    [
        {"prompt_version": "p@2"},
        {"prompt_sha256": "b" * 64},
        {"call_ordinal": 1},
    ],
)
def test_key_changes_with_prompt_and_ordinal(change: dict) -> None:
    assert _key({"x": 1}) != _key({"x": 1}, **change)


def test_key_changes_with_input() -> None:
    assert _key({"x": 1}) != _key({"x": 2})


def test_key_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        _key({"x": float("nan")})


# --- fake inner provider + record/replay ------------------------------------------------------


class Scripted:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)

    async def generate_structured(self, **kw: Any) -> LLMResult:
        item = self.responses.pop(0)
        usage = TaskUsage(input_tokens=10, output_tokens=5, llm_calls=1)
        try:
            out = parse_output(kw["output_schema"], item)
        except OutputParseError as exc:
            raise LLMOutputInvalidError(
                exc.reason, usage=usage, model_id="m-1", raw_response=exc.raw
            ) from None
        return LLMResult(output=out, usage=usage, model_id="m-1", latency_ms=1)


def _call_kwargs(input_data: dict, ordinal: int = 0) -> dict[str, Any]:
    return {
        "task": "classify",
        "instructions": "be good",
        "prompt_version": "p@1",
        "prompt_sha256": "a" * 64,
        "input_data": input_data,
        "output_schema": Out,
        "budget": TokenBudget(1000),
        "call_ordinal": ordinal,
        "key_context": CTX,
    }


async def test_replay_miss_reports_key(tmp_path: Path) -> None:
    with pytest.raises(ReplayMissError) as info:
        await ReplayLLMProvider(tmp_path).generate_structured(**_call_kwargs({"x": 1}))
    assert info.value.expected_key == _key({"x": 1})


async def test_record_then_replay(tmp_path: Path) -> None:
    recorder = RecordingProvider(Scripted("not json", VALID))
    with pytest.raises(LLMOutputInvalidError):
        await recorder.generate_structured(**_call_kwargs({"x": 1}, 0))
    await recorder.generate_structured(**_call_kwargs({"x": 1}, 1))
    paths = recorder.write(tmp_path)
    assert [p.parent for p in paths] == [tmp_path / "sigint-agent" / "baseline-corroboration"] * 2
    stored = json.loads(paths[1].read_text())
    assert set(stored) == {"key_material", "response", "usage", "model_id"}
    for p in paths:
        assert API_KEY_PATTERN.search(p.read_text()) is None

    replay = ReplayLLMProvider(tmp_path)
    with pytest.raises(LLMOutputInvalidError) as info:  # recorded invalid output replays invalid
        await replay.generate_structured(**_call_kwargs({"x": 1}, 0))
    assert info.value.usage.replayed is True
    result = await replay.generate_structured(**_call_kwargs({"x": 1, "task_id": "tsk_9"}, 1))
    assert result.output == Out.model_validate(VALID)
    assert result.model_id == "replay:m-1"
    assert result.usage.replayed is True and result.usage.input_tokens == 10


async def test_replay_rejects_renamed_file(tmp_path: Path) -> None:
    recorder = RecordingProvider(Scripted(VALID))
    await recorder.generate_structured(**_call_kwargs({"x": 1}))
    (path,) = recorder.write(tmp_path)
    path.rename(path.with_name(_key({"x": 2}) + ".json"))
    with pytest.raises(ReplayMissError):
        await ReplayLLMProvider(tmp_path).generate_structured(**_call_kwargs({"x": 2}))


async def test_write_refuses_key_like_text(tmp_path: Path) -> None:
    leaky = VALID | {"label": "sk-" + "A" * 40}
    recorder = RecordingProvider(Scripted(leaky))
    await recorder.generate_structured(**_call_kwargs({"x": 1}))
    with pytest.raises(ValueError, match="API-key"):
        recorder.write(tmp_path)
    assert not any(tmp_path.rglob("*.json"))


def test_key_pattern_ignores_ordinary_words() -> None:
    assert API_KEY_PATTERN.search("high-risk-assessment-for-the-task-xxxxxxxxxxxx") is None
    assert API_KEY_PATTERN.search("token sk-proj_abcdefghijklmnopqrstuv") is not None


# --- strict schema / parse -------------------------------------------------------------------


def test_strict_schema_every_object_closed_and_required() -> None:
    schema = strict_json_schema(Out)
    objects = [schema, *schema["$defs"].values()]
    for node in objects:
        assert node["additionalProperties"] is False
        assert sorted(node["required"]) == sorted(node["properties"])
        assert "title" not in node
    assert "title" in schema["$defs"]["Inner"]["properties"]


def test_strict_schema_rejects_optional_field() -> None:
    class Opt(BaseModel):
        a: int = 1

    with pytest.raises(ValueError, match="required"):
        strict_json_schema(Opt)


def test_parse_error_is_content_free() -> None:
    with pytest.raises(OutputParseError) as info:
        parse_output(Out, {"label": "SECRET-TEXT", "score": "nope", "items": []})
    assert "SECRET-TEXT" not in info.value.reason and "score" in info.value.reason


# --- budget -----------------------------------------------------------------------------------


def test_budget_pre_and_post() -> None:
    b = TokenBudget(2000)
    assert b.estimate_tokens("abcde") == 2
    assert b.estimate_call("a" * 400) == 100 + 1024
    b.ensure_can_spend(2000)
    with pytest.raises(BudgetExceededError):
        b.ensure_can_spend(2001)
    b.charge(TaskUsage(input_tokens=1500, output_tokens=400, llm_calls=1))
    assert b.remaining == 100
    assert b.output_allowance(90, 8000) == 16
    with pytest.raises(BudgetExceededError) as info:
        b.charge(TaskUsage(input_tokens=100, output_tokens=1, llm_calls=1))
    assert info.value.usage is not None
    with pytest.raises(ValueError):
        TokenBudget(0)


# --- TLS context ------------------------------------------------------------------------------


@pytest.mark.parametrize("content", [None, b"", b"not a certificate\n"])
def test_ssl_context_without_usable_extra_ca(tmp_path: Path, content: bytes | None) -> None:
    baseline = build_ssl_context(None)
    path = tmp_path / "extra_ca"
    if content is not None:
        path.write_bytes(content)  # empty compose secret / junk file -> certifi only
    ctx = build_ssl_context(path)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode is ssl.CERT_REQUIRED and ctx.check_hostname
    assert len(ctx.get_ca_certs()) == len(baseline.get_ca_certs()) > 0


def test_ssl_context_appends_pem(tmp_path: Path) -> None:
    import certifi

    pem = Path(certifi.where()).read_text()
    first = pem[pem.index("-----BEGIN CERTIFICATE-----") :]
    first = first[: first.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
    path = tmp_path / "ca.pem"
    path.write_text(first + "\n")
    ctx = build_ssl_context(path)  # a duplicate root: loads without error, still verifying
    assert ctx.verify_mode is ssl.CERT_REQUIRED


# --- OpenAI provider mapping (fake client, no network) ---------------------------------------


class FakeResponses:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _response(status: str = "completed", text: str = json.dumps(VALID), refusal: bool = False):
    content = [SimpleNamespace(type="refusal" if refusal else "output_text")]
    return SimpleNamespace(
        status=status,
        output_text=text,
        output=[SimpleNamespace(type="message", content=content)],
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        model="gpt-x-2026",
    )


def _provider(result: Any):
    from nexus_shared.llm import OpenAIProvider

    responses = FakeResponses(result)

    async def close() -> None:
        return None

    client = SimpleNamespace(responses=responses, close=close)
    provider = OpenAIProvider("gpt-x", SecretStr("test-key"), client=client)  # type: ignore[arg-type]
    return provider, responses


async def test_openai_provider_success_sends_strict_format() -> None:
    provider, responses = _provider(_response())
    result = await provider.generate_structured(**_call_kwargs({"x": 1}))
    assert result.model_id == "gpt-x-2026" and result.usage.total_tokens == 18
    fmt = responses.kwargs["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert responses.kwargs["store"] is False
    assert responses.kwargs["instructions"] == "be good"
    assert responses.kwargs["max_output_tokens"] <= 1000


@pytest.mark.parametrize(
    "resp",
    [_response(refusal=True), _response(status="incomplete"), _response(text="{bad")],
)
async def test_openai_provider_invalid_outputs(resp: Any) -> None:
    provider, _ = _provider(resp)
    with pytest.raises(LLMOutputInvalidError) as info:
        await provider.generate_structured(**_call_kwargs({"x": 1}))
    assert info.value.usage.input_tokens == 11


async def test_openai_provider_maps_transport_errors() -> None:
    import httpx
    import openai

    req = httpx.Request("POST", "https://api.invalid/v1/responses")
    provider, _ = _provider(openai.APIConnectionError(request=req))
    with pytest.raises(LLMUnavailableError):
        await provider.generate_structured(**_call_kwargs({"x": 1}))


def test_openai_provider_requires_key() -> None:
    from nexus_shared.llm import OpenAIProvider

    with pytest.raises(ValueError):
        OpenAIProvider("gpt-x", SecretStr(""))
