"""Strict JSON Schema for structured outputs, and content-free output validation.

OpenAI strict mode (Structured Outputs) needs: every object ``additionalProperties: false``, every
property listed in ``required``. We generate the schema from the Pydantic model and enforce those
two rules ourselves instead of using the SDK's private ``openai.lib._pydantic`` helper.
Length/range rules are **not** relied on in the schema; agents re-check them after parsing (S2
spec §8.2).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

_DROP_KEYS = ("title", "default")


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema of ``model`` made strict-compatible. Raises if a field is optional."""
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        """Visit one schema node (not a ``properties`` map: its keys are field names)."""
        if not isinstance(node, dict):
            return
        for key in _DROP_KEYS:
            node.pop(key, None)
        if node.get("type") == "object":
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        for container in ("properties", "$defs"):
            for sub in node.get(container, {}).values():
                walk(sub)
        for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            for sub in node.get(key, []):
                walk(sub)
        walk(node.get("items"))

    for name, field in model.model_fields.items():
        if not field.is_required():
            raise ValueError(f"strict schema: field {name!r} must be required")
    walk(schema)
    return schema


def describe_validation_error(exc: ValidationError, limit: int = 5) -> str:
    """Location + error type only; never the offending input (it is model output)."""
    parts = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('type', 'invalid')}")
    more = exc.error_count() - limit
    if more > 0:
        parts.append(f"(+{more} more)")
    return "; ".join(parts)


class OutputParseError(ValueError):
    """Raised by :func:`parse_output`; ``reason`` is content-free, ``raw`` is the parsed JSON (or
    the text when it is not JSON)."""

    def __init__(self, reason: str, raw: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


def parse_output(output_schema: type[BaseModel], raw: Any) -> BaseModel:
    """Validate ``raw`` (JSON text or already-decoded JSON) against ``output_schema``."""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise OutputParseError("output is not valid JSON", raw) from None
    try:
        return output_schema.model_validate(data)
    except ValidationError as exc:
        raise OutputParseError(
            "output failed schema validation: " + describe_validation_error(exc), data
        ) from None
