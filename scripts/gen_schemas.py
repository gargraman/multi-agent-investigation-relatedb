"""Export JSON Schema for every registered contract model.

Usage:
    uv run python scripts/gen_schemas.py          # (re)write contracts/schemas/*.schema.json
    uv run python scripts/gen_schemas.py --check  # drift check: exit 1 if committed files differ

Source of truth is the Pydantic models listed in ``nexus_contracts.registry.SCHEMA_MODELS``.
Output is deterministic: keys sorted, 2-space indent, trailing newline. ``--check`` never writes;
it reports missing, changed and stale (unregistered) schema files and exits non-zero on any.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from nexus_contracts.registry import SCHEMA_MODELS

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO_ROOT / "contracts" / "schemas"
SUFFIX = ".schema.json"


def render_schemas() -> dict[str, str]:
    """Return {filename: rendered JSON text} for every registered model."""
    rendered: dict[str, str] = {}
    for name, model in sorted(SCHEMA_MODELS.items()):
        schema = model.model_json_schema()
        rendered[f"{name}{SUFFIX}"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return rendered


def write(rendered: dict[str, str]) -> int:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, text in rendered.items():
        (SCHEMA_DIR / filename).write_text(text, encoding="utf-8")
        print(f"wrote {SCHEMA_DIR.relative_to(REPO_ROOT) / filename}")
    for stale in sorted(set(_existing()) - set(rendered)):
        print(f"warning: stale schema file not in registry: {stale} (delete it)", file=sys.stderr)
    return 0


def check(rendered: dict[str, str]) -> int:
    problems: list[str] = []
    existing = _existing()
    for filename, expected in rendered.items():
        path = SCHEMA_DIR / filename
        if filename not in existing:
            problems.append(f"MISSING  {filename}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            diff = difflib.unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=f"committed/{filename}",
                tofile=f"generated/{filename}",
            )
            problems.append(f"CHANGED  {filename}\n{''.join(diff)}")
    for stale in sorted(set(existing) - set(rendered)):
        problems.append(f"STALE    {stale} (no registered model)")

    if problems:
        print("Schema drift detected:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("Run `make schemas` and commit the result.", file=sys.stderr)
        return 1
    print(f"schemas up to date ({len(rendered)} file(s))")
    return 0


def _existing() -> list[str]:
    if not SCHEMA_DIR.is_dir():
        return []
    return sorted(p.name for p in SCHEMA_DIR.glob(f"*{SUFFIX}"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail on drift; do not write")
    args = parser.parse_args(argv)
    rendered = render_schemas()
    return check(rendered) if args.check else write(rendered)


if __name__ == "__main__":
    sys.exit(main())
