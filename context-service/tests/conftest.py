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
GEOINT_TOKEN = "g" * 40
GEOINT = {"Authorization": f"Bearer {GEOINT_TOKEN}"}


def make_settings(tmp_path: Path, **kw: Any) -> ContextServiceSettings:
    return ContextServiceSettings(
        service_token="c" * 40, relate_store_path=tmp_path / "store", **kw
    )


def create_body(investigation_id: str, **kw: Any) -> dict[str, Any]:
    body = ContextInvestigationCreate.from_manifest(investigation_id, load_manifest(MANIFEST), **kw)
    return body.model_dump(mode="json")


def _sha(evidence_id: str) -> str:
    return next(e.sha256 for e in load_manifest(MANIFEST).evidence if e.id == evidence_id)


def evidence_ref(evidence_id: str, owner: str = "sigint-agent") -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "sha256": _sha(evidence_id),
        "owner_node_id": owner,
        "summary": f"Contact metadata from {evidence_id}.",
    }


def finding(
    a: str = "HDL-ALPHA-1",
    b: str = "HDL-CHARLIE-1",
    evidence: tuple[str, ...] = ("ev-sig-001", "ev-sig-002"),
    day: int = 3,
    **kw: Any,
) -> dict[str, Any]:
    """A valid SIGINT finding: two LINKED_TO(CONTACT) assertions inside the finding window."""
    body: dict[str, Any] = {
        "kind": "OBSERVATION",
        "statement": f"{a} and {b} were in recurring contact.",
        "confidence": 0.81234,
        "confidence_band": "HIGH",
        "classification": "DEMO-LOW",
        "about": [a, b],
        "evidence": [evidence_ref(e) for e in evidence],
        "assertions": [
            {
                "type": "LINKED_TO",
                "subject": a,
                "object": b,
                "link_type": "CONTACT",
                "valid_from": f"2026-11-{day:02d}T10:00:00Z",
                "valid_until": f"2026-11-{day:02d}T10:05:00Z",
            },
            {
                "type": "LINKED_TO",
                "subject": a,
                "object": b,
                "link_type": "CONTACT",
                "valid_from": f"2026-11-{day:02d}T14:00:00.250000Z",
                "valid_until": f"2026-11-{day:02d}T14:10:00Z",
            },
        ],
        "valid_from": f"2026-11-{day:02d}T10:00:00Z",
        "valid_until": f"2026-11-{day:02d}T14:10:00Z",
        "reasoning_summary": "Two contacts within one day.",
        "model_id": "replay:gpt-test",
        "prompt_version": "sigint-classify@1.0.0",
        "schema_version": "1.0.0",
        "synthetic": True,
    }
    body.update(kw)
    return body


def batch_body(
    investigation_id: str, task_id: str = "tsk_one", attempt: int = 1, findings: Any = None
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "investigation_id": investigation_id,
        "attempt": attempt,
        "findings": findings
        if findings is not None
        else [finding(), finding("HDL-BRAVO-1", "HDL-DELTA-1", ("ev-sig-003",), day=4)],
    }


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def app(tmp_path: Path, exporter: InMemorySpanExporter) -> FastAPI:
    # sigint-agent is registered so 403 (valid token, disallowed op) is testable.
    registry = TokenRegistry(
        {"orchestrator": ORCH_TOKEN, "sigint-agent": SIGINT_TOKEN, "geoint-agent": GEOINT_TOKEN}
    )
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
