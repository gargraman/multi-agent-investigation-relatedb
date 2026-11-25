"""Replay and recording providers (spec §13.2, ADR-006 E1-E8, S2 spec §8.5).

Recording file: ``<replay_root>/<agent_id>/<scenario_id>/<key>.json`` =
``{key_material, response, usage, model_id}``.

- ``response`` is the model output as JSON (or the raw text when it was not JSON), so a recorded
  *invalid* output replays the same failure and the repair call (``call_ordinal`` 1) replays too.
- ``usage`` is ``{input_tokens, output_tokens, llm_calls}`` of the recorded call. Replay reports it
  with ``replayed: true`` so the budget path still runs (E7). Latency is real file-read time,
  never simulated.
- No headers, no auth, no key: only the four members above are ever written (E8). ``write``
  additionally refuses any file that matches an API-key pattern.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nexus_contracts.a2a.task import TaskUsage
from nexus_shared.llm.base import LLMProvider, LLMResult
from nexus_shared.llm.budget import TokenBudget
from nexus_shared.llm.errors import LLMOutputInvalidError, ReplayMissError
from nexus_shared.llm.keys import ReplayKeyContext, build_key_material, replay_key
from nexus_shared.llm.schema import OutputParseError, parse_output

REPLAY_MODEL_PREFIX = "replay:"

API_KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")
"""OpenAI-style secret key. The look-behind avoids false hits on words such as ``risk-``."""


def recording_path(replay_root: Path, agent_id: str, scenario_id: str, key: str) -> Path:
    return replay_root / agent_id / scenario_id / f"{key}.json"


class ReplayLLMProvider:
    """Serves recorded responses. Selected only by ``NEXUS_LLM_MODE=replay``; never a fallback."""

    def __init__(self, replay_root: Path) -> None:
        self.replay_root = replay_root

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
        started = time.monotonic()
        material = build_key_material(
            key_context,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
            call_ordinal=call_ordinal,
            input_data=input_data,
        )
        key = replay_key(material)
        path = recording_path(self.replay_root, key_context.agent_id, key_context.scenario_id, key)
        if not path.is_file():
            raise ReplayMissError(key, material)
        record = json.loads(path.read_text(encoding="utf-8"))
        if replay_key(record.get("key_material", {})) != key:
            raise ReplayMissError(key, material)  # stored material does not match its file name
        u = record["usage"]
        usage = TaskUsage(
            input_tokens=int(u["input_tokens"]),
            output_tokens=int(u["output_tokens"]),
            llm_calls=int(u.get("llm_calls", 1)),
            replayed=True,
        )
        model_id = REPLAY_MODEL_PREFIX + str(record["model_id"])
        try:
            output = parse_output(output_schema, record["response"])
        except OutputParseError as exc:
            raise LLMOutputInvalidError(
                exc.reason, usage=usage, model_id=model_id, raw_response=exc.raw
            ) from None
        latency_ms = int((time.monotonic() - started) * 1000)
        return LLMResult(output=output, usage=usage, model_id=model_id, latency_ms=latency_ms)


@dataclass(frozen=True, slots=True)
class Recording:
    key: str
    agent_id: str
    scenario_id: str
    key_material: dict[str, Any]
    response: Any
    usage: dict[str, int]
    model_id: str

    def to_json(self) -> str:
        body = {
            "key_material": self.key_material,
            "response": self.response,
            "usage": self.usage,
            "model_id": self.model_id,
        }
        return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class RecordingProvider:
    """Wraps a (live) provider and records every call, valid or not. ``write`` persists them;
    ``record_replay.py`` calls it only after the oracle gate passes (ADR-006 E5)."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.recordings: list[Recording] = []

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
        material = build_key_material(
            key_context,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
            call_ordinal=call_ordinal,
            input_data=input_data,
        )

        def record(response: Any, usage: TaskUsage, model_id: str) -> None:
            self.recordings.append(
                Recording(
                    key=replay_key(material),
                    agent_id=key_context.agent_id,
                    scenario_id=key_context.scenario_id,
                    key_material=material,
                    response=response,
                    usage={
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "llm_calls": usage.llm_calls,
                    },
                    model_id=model_id.removeprefix(REPLAY_MODEL_PREFIX),
                )
            )

        try:
            result = await self.inner.generate_structured(
                task=task,
                instructions=instructions,
                prompt_version=prompt_version,
                prompt_sha256=prompt_sha256,
                input_data=input_data,
                output_schema=output_schema,
                budget=budget,
                call_ordinal=call_ordinal,
                key_context=key_context,
            )
        except LLMOutputInvalidError as exc:
            if exc.raw_response is not None:
                record(exc.raw_response, exc.usage, exc.model_id)
            raise
        record(result.output.model_dump(mode="json"), result.usage, result.model_id)
        return result

    def write(self, replay_root: Path) -> list[Path]:
        """Write every recording; refuses (writes nothing) if any matches an API-key pattern."""
        rendered = [(r, r.to_json()) for r in self.recordings]
        for _, text in rendered:
            if API_KEY_PATTERN.search(text):
                raise ValueError("refusing to write a recording that matches an API-key pattern")
        paths = []
        for rec, text in rendered:
            path = recording_path(replay_root, rec.agent_id, rec.scenario_id, rec.key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            paths.append(path)
        return paths
