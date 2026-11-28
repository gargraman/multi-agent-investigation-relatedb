"""S4a spec §12: span attributes ``nexus.audit.outcome``, ``nexus.filtered_above_clearance`` and
``nexus.assessment.rejected_edges``. Counts and outcome only: no ids, no statements (NFR-202)."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from nexus_context_service.main import create_app
from nexus_shared.security import TokenRegistry
from nexus_shared.testing import InMemorySpanExporter

from . import test_assessments as base
from .conftest import (
    GEOINT,
    GEOINT_TOKEN,
    ORCH_TOKEN,
    SIGINT_TOKEN,
    batch_body,
    client_for,
    make_settings,
)
from .test_assessment_edges import edge, submit
from .test_findings_batch import _geoint_finding

seeded = base.seeded


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
async def app(tmp_path: Path, exporter: InMemorySpanExporter) -> FastAPI:
    registry = TokenRegistry(
        {
            "orchestrator": ORCH_TOKEN,
            "sigint-agent": SIGINT_TOKEN,
            "geoint-agent": GEOINT_TOKEN,
            "analysis-agent": base.ANALYSIS_TOKEN,
        }
    )
    return create_app(make_settings(tmp_path), token_registry=registry, span_exporter=exporter)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app), client_for(app) as c:
        yield c


def _attrs(exporter: InMemorySpanExporter, key: str) -> list[Any]:
    return [s.attributes[key] for s in exporter.get_finished_spans() if key in s.attributes]


async def test_filtered_analysis_read_sets_outcome_and_count(
    client: httpx.AsyncClient, exporter: InMemorySpanExporter, seeded: base.Seeded
) -> None:
    exporter.clear()
    await base.retrieve(client)  # seeded.high is DEMO-HIGH: hidden from analysis, counted
    assert _attrs(exporter, "nexus.audit.outcome") == ["FILTERED"]
    assert _attrs(exporter, "nexus.filtered_above_clearance") == [1]
    for s in exporter.get_finished_spans():
        assert seeded.high not in str(dict(s.attributes))


async def test_unfiltered_read_sets_success_and_zero(
    client: httpx.AsyncClient, exporter: InMemorySpanExporter, seeded: base.Seeded
) -> None:
    exporter.clear()
    r = await client.get(f"/v1/investigations/{base.INV_B}/context", headers=base.analysis())
    assert r.status_code == 200, r.text
    assert _attrs(exporter, "nexus.audit.outcome") == ["SUCCESS"]
    assert _attrs(exporter, "nexus.filtered_above_clearance") == [0]


async def test_assessment_sets_rejected_edge_count(
    client: httpx.AsyncClient, exporter: InMemorySpanExporter, seeded: base.Seeded
) -> None:
    await base.retrieve(client)
    geo = batch_body(base.INV, task_id="tsk_geo3", findings=[_geoint_finding("ENT-BRAVO")])
    [f3] = (await base._post_ok(client, "/v1/findings:batch", geo, GEOINT))["finding_ids"]
    good = edge("CORROBORATES", seeded.f1, seeded.f2)
    bad = edge("CORROBORATES", seeded.f1, f3)  # visible but never retrieved
    exporter.clear()
    await submit(client, [seeded.f1], [good, bad], base.TASK, do_retrieve=False)
    assert _attrs(exporter, "nexus.assessment.rejected_edges") == [1]
    for s in exporter.get_finished_spans():
        assert f3 not in str(dict(s.attributes))
