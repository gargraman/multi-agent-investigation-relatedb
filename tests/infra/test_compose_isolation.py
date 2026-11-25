"""AC5 compose lint: findings persist only through the Context Service (spec §17.3 AC5, §14.2).

Static checks on infrastructure/docker/compose.yaml. They prove the *configuration*, not runtime
isolation: Compose networks on one host simulate segmentation (spec §14.2)."""

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


def test_agents_mount_only_their_own_fixtures(services: dict) -> None:
    for name, svc in services.items():
        if not name.endswith("-agent"):
            continue
        kind = name.removesuffix("-agent")
        fixture_mounts = [s for s in _volume_sources(svc) if "data/fixtures" in s]
        assert fixture_mounts, f"{name} mounts no fixtures"
        assert all(s.rstrip("/").endswith(f"data/fixtures/{kind}") for s in fixture_mounts), (
            name,
            fixture_mounts,
        )
        replay = [s for s in _volume_sources(svc) if "data/replay" in s]
        assert all(s.rstrip("/").endswith(f"data/replay/{name}") for s in replay), (name, replay)


def test_read_only_data_mounts(services: dict) -> None:
    for name, svc in services.items():
        for v in svc.get("volumes", []):
            if isinstance(v, str) and "/data/" in v.split(":", 1)[0] and v.startswith("../"):
                assert v.endswith(":ro"), (name, v)
