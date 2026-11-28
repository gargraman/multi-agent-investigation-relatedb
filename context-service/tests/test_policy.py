"""policy.yaml drives the operation allowlist and clearance (S4a FR-201, spec §14.1)."""

from pathlib import Path

import httpx
import pytest

from nexus_context_service.main import create_app
from nexus_context_service.policy import OPERATIONS, Policy, PolicyError
from nexus_contracts.common import Classification
from nexus_shared.security import TokenRegistry
from nexus_shared.testing import InMemorySpanExporter

from .conftest import GEOINT_TOKEN, ORCH_TOKEN, SIGINT_TOKEN, client_for, make_settings

ANALYSIS_TOKEN = "a" * 40


def test_shipped_policy_matches_spec() -> None:
    policy = Policy.load(None)
    assert policy.allows("analysis-agent", "investigations.context")
    assert not policy.allows("analysis-agent", "investigations.links")
    assert policy.allows("orchestrator", "audit.read")
    assert not policy.allows("nobody", "audit.read")
    assert policy.read_rank("analysis-agent") == 2
    assert policy.read_rank("orchestrator") == 3
    assert policy.read_rank("sigint-agent") == 0
    assert policy.write_clearance("geoint-agent") is Classification.DEMO_MID
    assert policy.write_clearance("sigint-agent") is Classification.DEMO_HIGH
    assert policy.write_clearance("orchestrator") is None
    assert len(OPERATIONS) == 13


def test_shipped_policy_is_package_data() -> None:
    from importlib import resources

    res = resources.files("nexus_context_service").joinpath("config/policy.yaml")
    assert res.is_file()


def test_policy_path_overrides_the_shipped_file(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("callers:\n  analysis-agent:\n    read: DEMO-LOW\n    operations: [search]\n")
    policy = Policy.load(path)
    assert policy.read_rank("analysis-agent") == 1
    assert not policy.allows("orchestrator", "audit.read")


@pytest.mark.parametrize(
    "text",
    [
        "callers: [unclosed",
        "",
        "- just\n- a list\n",
        "callers:\n  analysis-agent:\n    read: DEMO-ULTRA\n    operations: [search]\n",
        "callers:\n  analysis-agent:\n    write: DEMO-ULTRA\n    operations: [search]\n",
        "callers:\n  analysis-agent:\n    read: DEMO-MID\n    operations: [nope]\n",
        "callers:\n  analysis-agent:\n    read: DEMO-MID\n    operations: search\n",
        "callers:\n  analysis-agent:\n    read: DEMO-MID\n    operations: [search]\n    extra: 1\n",
        "callers: {}\nextra: 1\n",
        "extra: 1\n",
        "callers:\n  analysis-agent: DEMO-MID\n",
    ],
    ids=[
        "not-yaml",
        "empty",
        "not-a-mapping",
        "unknown-read-class",
        "unknown-write-class",
        "unknown-operation",
        "operations-not-a-list",
        "unknown-caller-key",
        "unknown-top-level-key",
        "missing-callers",
        "caller-not-a-mapping",
    ],
)
def test_malformed_policy_fails(tmp_path: Path, text: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    with pytest.raises(PolicyError):
        Policy.load(path)


def test_missing_policy_file_fails(tmp_path: Path) -> None:
    with pytest.raises(PolicyError):
        Policy.load(tmp_path / "absent.yaml")


def test_service_refuses_to_start_on_bad_policy(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("callers:\n  orchestrator:\n    operations: [nope]\n")
    registry = TokenRegistry({"orchestrator": ORCH_TOKEN})
    with pytest.raises(PolicyError):
        create_app(
            make_settings(tmp_path, policy_path=bad),
            token_registry=registry,
            span_exporter=InMemorySpanExporter(),
        )


async def test_analysis_can_read_context_now(tmp_path: Path) -> None:
    registry = TokenRegistry(
        {
            "orchestrator": ORCH_TOKEN,
            "sigint-agent": SIGINT_TOKEN,
            "geoint-agent": GEOINT_TOKEN,
            "analysis-agent": ANALYSIS_TOKEN,
        }
    )
    app = create_app(
        make_settings(tmp_path), token_registry=registry, span_exporter=InMemorySpanExporter()
    )
    async with app.router.lifespan_context(app), client_for(app) as c:
        r: httpx.Response = await c.get(
            "/v1/investigations/inv_none/context",
            headers={
                "Authorization": f"Bearer {ANALYSIS_TOKEN}",
                "X-Nexus-Task-Id": "tsk_one",
            },
        )
    assert r.status_code in (200, 404)
