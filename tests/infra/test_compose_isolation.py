"""AC5 compose lint: findings persist only through the Context Service (spec §17.3 AC5, §14.2).

Static checks on infrastructure/docker/compose.yaml. They prove the *configuration*, not runtime
isolation: Compose networks on one host simulate segmentation (spec §14.2)."""

import json
from pathlib import Path

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "infrastructure/docker/compose.yaml"
STORE_VOLUME = "relate-store"


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _volume_sources(svc: dict) -> list[str]:
    out = []
    for v in svc.get("volumes", []):
        out.append(v.split(":", 1)[0] if isinstance(v, str) else v.get("source", ""))
    return out


def test_only_context_service_mounts_the_store(services: dict) -> None:
    mounting = sorted(n for n, s in services.items() if STORE_VOLUME in _volume_sources(s))
    assert mounting == ["context-service"]


def test_no_service_bind_mounts_a_store_path(services: dict) -> None:
    for name, svc in services.items():
        if name == "context-service":
            continue
        for src in _volume_sources(svc):
            assert "relate" not in src.lower(), (name, src)


def test_context_service_has_no_egress_network(services: dict) -> None:
    assert services["context-service"]["networks"] == ["nexus-context"]


def test_frontend_cannot_reach_context_or_agents(services: dict) -> None:
    assert services["frontend"]["networks"] == ["nexus-app"]


def test_agent_volume_sources_are_an_exact_allowlist(services: dict) -> None:
    """Every ``*-agent`` mounts exactly: its state volume, the scenario manifests, its own
    fixtures and its own replay recordings. Anything broader (e.g. ``../../data``) fails."""
    agents = [n for n in services if n.endswith("-agent")]
    assert agents, "no *-agent services found"
    for name in agents:
        short = name.removesuffix("-agent")
        allowed = {
            f"{short}-state",
            "../../data/scenarios",
            f"../../data/fixtures/{short}",
            f"../../data/replay/{name}",
        }
        sources = [s.rstrip("/") for s in _volume_sources(services[name])]
        assert sorted(sources) == sorted(allowed), (name, sources)


def test_read_only_data_mounts(services: dict) -> None:
    for name, svc in services.items():
        for v in svc.get("volumes", []):
            if isinstance(v, str) and "/data/" in v.split(":", 1)[0] and v.startswith("../"):
                assert v.endswith(":ro"), (name, v)


def test_geoint_networks_are_context_and_egress(services: dict) -> None:
    assert sorted(services["geoint-agent"]["networks"]) == ["nexus-context", "nexus-egress"]


def test_orchestrator_enables_both_specialists(services: dict) -> None:
    orch = services["orchestrator"]
    env = orch["environment"]
    assert env["NEXUS_ENABLED_AGENTS"] == "sigint-agent,geoint-agent"
    assert json.loads(env["NEXUS_AGENT_URLS"]) == {
        "sigint-agent": "http://sigint-agent:8000",
        "geoint-agent": "http://geoint-agent:8000",
    }
    assert orch["depends_on"]["geoint-agent"] == {"condition": "service_healthy"}
    assert orch["depends_on"]["sigint-agent"] == {"condition": "service_healthy"}
