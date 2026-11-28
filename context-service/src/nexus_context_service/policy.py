"""The authorization policy: who may call which operation, and at what classification.

``config/policy.yaml`` ships as package data (the runtime image copies only ``.venv``) and is
loaded with ``importlib.resources``; ``policy_path`` overrides it (tests, ops). The file is
validated strictly at startup: a malformed policy must stop the service, never widen access.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from nexus_contracts.common import Classification

# Every operation a route can authorize (spec §14.1, one per row).
OPERATIONS: frozenset[str] = frozenset(
    {
        "investigations.create",
        "investigations.close",
        "investigations.context",
        "assessments.provenance",
        "evidence.retract",
        "entities.contradictions",
        "investigations.links",
        "investigations.graph",
        "audit.read",
        "findings.submit",
        "entities.create",
        "search",
        "assessments.create",
    }
)

_CALLER_KEYS = frozenset({"read", "write", "operations"})


class PolicyError(ValueError):
    """The policy file is missing, unparseable, or names something unknown."""


@dataclass(frozen=True)
class CallerPolicy:
    operations: frozenset[str]
    read: Classification | None = None  # max classification the caller may read
    write: Classification | None = None  # max classification the caller may assert


def _classification(caller: str, key: str, value: Any) -> Classification:
    try:
        return Classification(value)
    except ValueError:
        raise PolicyError(f"callers.{caller}.{key}: unknown classification") from None


def _parse_caller(caller: str, raw: Any) -> CallerPolicy:
    if not isinstance(raw, dict):
        raise PolicyError(f"callers.{caller}: must be a mapping")
    unknown = set(raw) - _CALLER_KEYS
    if unknown:
        raise PolicyError(f"callers.{caller}: unknown key {sorted(map(str, unknown))[0]!r}")
    ops = raw.get("operations")
    if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops):
        raise PolicyError(f"callers.{caller}.operations: must be a list of operation names")
    bad = sorted(set(ops) - OPERATIONS)
    if bad:
        raise PolicyError(f"callers.{caller}.operations: unknown operation {bad[0]!r}")
    read = _classification(caller, "read", raw["read"]) if "read" in raw else None
    write = _classification(caller, "write", raw["write"]) if "write" in raw else None
    return CallerPolicy(operations=frozenset(ops), read=read, write=write)


@dataclass(frozen=True)
class Policy:
    callers: dict[str, CallerPolicy]

    @classmethod
    def load(cls, path: Path | None) -> Policy:
        """Load and validate; ``None`` loads the shipped package-data file."""
        try:
            if path is None:
                text = (
                    resources.files("nexus_context_service")
                    .joinpath("config/policy.yaml")
                    .read_text(encoding="utf-8")
                )
            else:
                text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PolicyError(f"policy file unreadable: {exc.strerror or exc}") from exc
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise PolicyError("policy file is not valid YAML") from exc
        if not isinstance(raw, dict):
            raise PolicyError("policy file must be a mapping with a 'callers' key")
        unknown = set(raw) - {"callers"}
        if unknown:
            raise PolicyError(f"unknown top-level key {sorted(map(str, unknown))[0]!r}")
        callers = raw.get("callers")
        if not isinstance(callers, dict):
            raise PolicyError("'callers' must be a mapping")
        return cls({str(name): _parse_caller(str(name), body) for name, body in callers.items()})

    def allows(self, caller: str, operation: str) -> bool:
        entry = self.callers.get(caller)
        return entry is not None and operation in entry.operations

    def read_rank(self, caller: str) -> int:
        """1-3 for a caller with a read clearance, 0 for one without."""
        entry = self.callers.get(caller)
        return entry.read.rank if entry is not None and entry.read is not None else 0

    def write_clearance(self, caller: str) -> Classification | None:
        entry = self.callers.get(caller)
        return entry.write if entry is not None else None
