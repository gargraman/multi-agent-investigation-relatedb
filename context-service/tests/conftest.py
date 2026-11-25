from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nexus_context_service.main import create_app
from nexus_context_service.settings import ContextServiceSettings
from nexus_contracts.api.context import ContextInvestigationCreate
from nexus_contracts.scenario.loader import load_manifest
from nexus_shared.security import TokenRegistry
from nexus_shared.testing import InMemorySpanExporter

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data/scenarios/baseline-corroboration/manifest.yaml"

ORCH_TOKEN = "o" * 40
SIGINT_TOKEN = "s" * 40
ORCH = {"Authorization": f"Bearer {ORCH_TOKEN}"}
SIGINT = {"Authorization": f"Bearer {SIGINT_TOKEN}"}


def make_settings(tmp_path: Path, **kw: Any) -> ContextServiceSettings:
    return ContextServiceSettings(
        service_token="c" * 40, relate_store_path=tmp_path / "store", **kw
    )


def create_body(investigation_id: str, **kw: Any) -> dict[str, Any]:
    body = ContextInvestigationCreate.from_manifest(investigation_id, load_manifest(MANIFEST), **kw)
    return body.model_dump(mode="json")


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def app(tmp_path: Path, exporter: InMemorySpanExporter) -> FastAPI:
    # sigint-agent is registered so 403 (valid token, disallowed op) is testable.
    registry = TokenRegistry({"orchestrator": ORCH_TOKEN, "sigint-agent": SIGINT_TOKEN})
    return create_app(make_settings(tmp_path), token_registry=registry, span_exporter=exporter)


def client_for(app: FastAPI, **kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, **kw), base_url="http://cs")


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app), client_for(app) as c:
        yield c


async def read(app: FastAPI, cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
    """Read back through the lane (the only connection)."""
    return await app.state.lane.run(
        "test.read", lambda conn: [dict(r) for r in conn.execute(cypher, params or {}).fetchall()]
    )
