"""Auth, health, lane, telemetry and startup guards."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from nexus_context_service.lane import LaneUnavailable, RelateLane
from nexus_context_service.main import create_app
from nexus_context_service.settings import DurabilityModeError, load_token_registry
from nexus_shared.security import TokenRegistry

from .conftest import ORCH, SIGINT, client_for, create_body, make_settings, read


async def test_auth_401_and_403(client, app: FastAPI) -> None:
    body = create_body("inv_auth")
    r = await client.post("/v1/investigations", json=body)
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["www-authenticate"] == "Bearer"
    r = await client.post(
        "/v1/investigations", json=body, headers={"Authorization": "Bearer " + "x" * 40}
    )
    assert r.status_code == 401
    r = await client.post(
        "/v1/investigations", json=body, headers={**SIGINT, "X-Nexus-Actor": "operator:demo"}
    )
    assert r.status_code == 403
    assert r.json()["status"] == 403
    assert await read(app, "MATCH (i:Investigation) RETURN i") == []
    r = await client.post(
        "/v1/investigations/inv_auth/close", json={"status": "FAILED"}, headers=SIGINT
    )
    assert r.status_code == 403

    # Denials are audited; X-Nexus-Actor is ignored from a non-orchestrator caller.
    denials = await read(
        app,
        "MATCH (a:AuditEvent {authz:'DENIED'}) "
        "RETURN a.caller AS caller, a.actor AS actor, a.operation AS op ORDER BY op",
    )
    assert denials == [
        {"caller": "sigint-agent", "actor": "sigint-agent", "op": "investigations.close"},
        {"caller": "sigint-agent", "actor": "sigint-agent", "op": "investigations.create"},
    ]


async def test_actor_header_only_trusted_from_orchestrator(client, app: FastAPI) -> None:
    r = await client.post("/v1/investigations", json=create_body("inv_act"), headers=ORCH)
    assert r.status_code == 201
    r = await client.post(
        "/v1/investigations/inv_act/close",
        json={"status": "CANCELLED"},
        headers={**ORCH, "X-Nexus-Actor": "operator:alice"},
    )
    assert r.status_code == 200
    rows = await read(
        app,
        "MATCH (a:AuditEvent)-[:CONCERNS]->(:Investigation {id:'inv_act'}) "
        "RETURN a.operation AS op, a.caller AS caller, a.actor AS actor ORDER BY op",
    )
    assert rows == [
        {"op": "investigations.close", "caller": "orchestrator", "actor": "operator:alice"},
        {"op": "investigations.create", "caller": "orchestrator", "actor": "orchestrator"},
    ]


async def test_readyz_and_livez(app: FastAPI) -> None:
    async with client_for(app) as c:
        assert (await c.get("/livez")).status_code == 200
        assert (await c.get("/readyz")).status_code == 503  # store not open yet
        r = await c.post("/v1/investigations", json=create_body("inv_r"), headers=ORCH)
        assert r.status_code == 503
        assert r.headers["content-type"].startswith("application/problem+json")
        async with app.router.lifespan_context(app):
            assert (await c.get("/readyz")).status_code == 200
            assert (await c.get("/livez")).status_code == 200
        assert (await c.get("/readyz")).status_code == 503  # closed on shutdown
        assert (await c.get("/livez")).status_code == 200


async def test_relate_tx_span_has_no_parameters(client, exporter) -> None:
    body = create_body("inv_span", objective="Secret objective text")
    assert (await client.post("/v1/investigations", json=body, headers=ORCH)).status_code == 201
    spans = [s for s in exporter.get_finished_spans() if s.name == "relate.tx"]
    ops = {s.attributes["db.operation"] for s in spans}
    assert {"schema.bootstrap", "investigations.create"} <= ops
    create_span = next(s for s in spans if s.attributes["db.operation"] == "investigations.create")
    assert dict(create_span.attributes) == {
        "db.system": "relate",
        "db.operation": "investigations.create",
    }
    assert not create_span.events
    # Child of the HTTP server span (one trace per request).
    server = [s for s in exporter.get_finished_spans() if s.kind.name == "SERVER"]
    assert any(create_span.context.trace_id == s.context.trace_id for s in server)
    for s in spans:
        values = " ".join(str(v) for v in s.attributes.values())
        assert "inv_span" not in values and "Secret" not in values


async def test_lane_rolls_back_partial_investigation(client, app: FastAPI) -> None:
    # Plant an entity whose uid collides with one the create will insert after the
    # Investigation node: the uid constraint fails mid-transaction.
    await app.state.lane.run(
        "test.plant",
        lambda conn: conn.execute("CREATE (:Entity {uid:'inv_rb:ENT-ALPHA'})"),
    )
    async with client_for(app, raise_app_exceptions=False) as c:
        r = await c.post("/v1/investigations", json=create_body("inv_rb"), headers=ORCH)
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/problem+json")
    assert await read(app, "MATCH (i:Investigation {id:'inv_rb'}) RETURN i") == []
    [c] = await read(app, "MATCH (e:Entity {investigation_id:'inv_rb'}) RETURN count(e) AS c")
    assert c["c"] == 0
    assert await read(app, "MATCH (a:AuditEvent) RETURN a") == []
    # The lane is healthy afterwards.
    r = await client.post("/v1/investigations", json=create_body("inv_ok"), headers=ORCH)
    assert r.status_code == 201


async def test_lane_rollback_on_callable_error(tmp_path: Path) -> None:
    lane = RelateLane(tmp_path / "db")
    await lane.start()
    try:

        def boom(conn):
            conn.execute("CREATE (:Investigation {id:'inv_x'})")
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await lane.run("test.boom", boom)
        rows = await lane.run(
            "test.read", lambda conn: conn.execute("MATCH (n) RETURN count(n) AS c").single()
        )
        assert rows["c"] == 0
    finally:
        await lane.close()
    with pytest.raises(LaneUnavailable):
        await lane.run("test.after", lambda conn: None)


async def test_store_survives_restart(tmp_path: Path) -> None:
    registry = TokenRegistry({"orchestrator": "o" * 40})
    for expected in (201, 200):
        app = create_app(make_settings(tmp_path), token_registry=registry, span_exporter=None)
        async with app.router.lifespan_context(app), client_for(app) as c:
            r = await c.post(
                "/v1/investigations",
                json=create_body("inv_p"),
                headers={"Authorization": "Bearer " + "o" * 40},
            )
            assert r.status_code == expected


def test_durability_guard_refuses_start(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RELATE_DURABILITY_MODE", "everysecond")
    with pytest.raises(ValidationError):
        make_settings(tmp_path)
    registry = TokenRegistry({"orchestrator": "o" * 40})
    with pytest.raises(DurabilityModeError):
        create_app(make_settings(tmp_path, relate_durability_mode=None), token_registry=registry)


async def test_durability_guard_in_lane(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RELATE_DURABILITY_MODE", "snapshot")
    with pytest.raises(DurabilityModeError):
        await RelateLane(tmp_path / "db").start()


def test_durability_full_is_accepted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RELATE_DURABILITY_MODE", "full")
    assert make_settings(tmp_path).relate_durability_mode == "full"


def test_token_registry_required_and_optional_callers() -> None:
    # Orchestrator and sigint-agent are required (FR-045); geoint-agent is optional until S3.
    with pytest.raises(ValueError):
        load_token_registry({"NEXUS_TOKEN_ORCHESTRATOR": "o" * 40})
    with pytest.raises(ValueError):
        load_token_registry({"NEXUS_TOKEN_SIGINT_AGENT": "s" * 40})
    env = {"NEXUS_TOKEN_ORCHESTRATOR": "o" * 40, "NEXUS_TOKEN_SIGINT_AGENT": "s" * 40}
    assert set(load_token_registry(env).tokens) == {"orchestrator", "sigint-agent"}
    full = {**env, "NEXUS_TOKEN_GEOINT_AGENT": "g" * 40}
    assert set(load_token_registry(full).tokens) == {"orchestrator", "sigint-agent", "geoint-agent"}
    with pytest.raises(ValueError):  # set but too short is still a startup error
        load_token_registry({**env, "NEXUS_TOKEN_GEOINT_AGENT": "short"})


def test_telemetry_env_forced_off() -> None:
    import os

    assert os.environ["DO_NOT_TRACK"] == "1"
    assert os.environ["RELATE_TELEMETRY"] == "0"


async def test_periodic_flush_runs_on_lane(tmp_path: Path) -> None:
    import asyncio

    lane = RelateLane(tmp_path / "db", flush_interval_s=0.01)
    await lane.start()
    try:
        await lane.run("test.write", lambda conn: conn.execute("CREATE (:Investigation {id:'x'})"))
        await asyncio.sleep(0.05)  # several flushes interleave with lane work
        row = await lane.run(
            "test.read", lambda conn: conn.execute("MATCH (n) RETURN count(n) AS c").single()
        )
        assert row["c"] == 1
    finally:
        await lane.close()
