"""Registry of contract models whose JSON Schema is exported and committed.

``scripts/gen_schemas.py`` writes ``contracts/schemas/<name>.schema.json`` for every entry, and
its ``--check`` mode (run by the test suite) fails if the committed files drift.
Add a model here when it becomes part of a public contract.
"""

from pydantic import BaseModel

from nexus_contracts.api.problem import ProblemDetails

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "problem-details": ProblemDetails,
}
