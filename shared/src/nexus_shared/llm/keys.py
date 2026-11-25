"""Replay key material and canonical JSON (spec §13.2 "Replay lookup key", ADR-006 E1/E2).

A recording is found by ``sha256(canonical_json(key_material))`` where::

    key_material = {scenario_id, agent_id, task_type, prompt_version, prompt_sha256,
                    schema_version, call_ordinal, normalized_input}

``normalized_input`` is the exact ``input_data`` given to the provider with volatile fields
removed (generated ids, attempt, trace context, budgets, deadlines). The model id is deliberately
**not** in the key (S2 spec §11): a pinned snapshot is stored next to the response instead.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "task_id",
        "investigation_id",
        "attempt",
        "trace_context",
        "traceparent",
        "tracestate",
        "token_budget",
        "budget",
        "deadline",
        "created_at",
        "updated_at",
        "finding_ids",
    }
)
"""Keys dropped wherever they appear (any nesting depth)."""

GENERATED_ID = re.compile(r"^(inv|tsk|fnd|asm|sub)_[A-Za-z0-9_-]{1,64}$")
"""Per-run generated ids (spec §13.2): values matching this are dropped from dicts and lists."""

FLOAT_DIGITS = 6
"""Fixed float formatting: floats are rounded to 6 decimals before hashing."""


@dataclass(frozen=True, slots=True)
class ReplayKeyContext:
    """The per-task, non-input part of the key material."""

    scenario_id: str
    agent_id: str
    task_type: str
    schema_version: str


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = unicodedata.normalize("NFC", str(k))
            if key in VOLATILE_KEYS or (isinstance(v, str) and GENERATED_ID.match(v)):
                continue
            out[key] = _normalize(v)
        return out
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value if not (isinstance(v, str) and GENERATED_ID.match(v))]
    return _scalar(value)


def _scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float in replay input")
        return round(value, FLOAT_DIGITS)
    if isinstance(value, Enum):
        return _scalar(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    raise TypeError(f"unsupported type in replay input: {type(value).__name__}")


def normalize_input(input_data: Mapping[str, Any]) -> dict[str, Any]:
    """Strip volatile fields (§13.2) and normalise scalars (NFC strings, rounded floats,
    ISO datetimes). The result is plain JSON data."""
    return _normalize(input_data)


def canonical_json(obj: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 (not ASCII-escaped). Floats must already be rounded."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def build_key_material(
    key_context: ReplayKeyContext,
    *,
    prompt_version: str,
    prompt_sha256: str,
    call_ordinal: int,
    input_data: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "scenario_id": key_context.scenario_id,
        "agent_id": key_context.agent_id,
        "task_type": key_context.task_type,
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "schema_version": key_context.schema_version,
        "call_ordinal": call_ordinal,
        "normalized_input": normalize_input(input_data),
    }


def replay_key(key_material: Mapping[str, Any]) -> str:
    """``sha256(canonical_json(key_material))`` as lowercase hex."""
    return hashlib.sha256(canonical_json(_normalize(key_material)).encode("utf-8")).hexdigest()


def prompt_sha256(prompt_text: str) -> str:
    """Hash of the prompt file's contents (ADR-006 E2)."""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
