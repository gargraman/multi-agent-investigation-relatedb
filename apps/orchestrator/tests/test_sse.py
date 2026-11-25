"""SSE timeline (ADR-005 D3): replay from Last-Event-ID, then live, no gap and no duplicate.

Runs the app under a real uvicorn server because httpx's ASGITransport buffers whole bodies.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
from orch_testkit import SCENARIO, FakeContextService, runtime, served

from nexus_contracts.events.timeline import TimelineEvent, TimelineEventType
from nexus_orchestrator.ids import utcnow


async def sse_events(resp: httpx.Response) -> AsyncIterator[dict]:
    """Minimal SSE parser: yields {'id','event','data'} dicts and {'comment'} for pings."""
    current: dict = {}
    async for line in resp.aiter_lines():
        if line == "":
            if current:
                yield current
                current = {}
            continue
        if line.startswith(":"):
            yield {"comment": line[1:].strip()}
            continue
        key, _, value = line.partition(":")
        current[key] = value.removeprefix(" ")


async def append(app, investigation_id: str, n: int) -> list[TimelineEvent]:
    rt = runtime(app)
    out = []
    for i in range(n):
        async with rt.db.transaction() as tx:
            out.append(
                await rt.repo.append_event(
                    tx, investigation_id, TimelineEventType.LLM_REPLAYED, {"i": i}, utcnow()
                )
            )
    return out


async def collect(events: AsyncIterator[dict], count: int, timeout: float = 5) -> list[dict]:
    """Take ``count`` data events (skipping ping comments) from one shared parser."""
    got: list[dict] = []

    async def run() -> None:
        while len(got) < count:
            ev = await anext(events)
            if "comment" not in ev:
                got.append(ev)

    await asyncio.wait_for(run(), timeout)
    return got


async def test_replay_from_last_event_id_then_live_without_duplicates(make_app) -> None:
    app = make_app(FakeContextService())
    async with served(app) as base, httpx.AsyncClient(base_url=base, timeout=10) as client:
        inv = (await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO})).json()
        first = await client.get(f"/api/v1/investigations/{inv['id']}/timeline")
        created_seq = first.json()["items"][0]["seq"]
        later = await append(app, inv["id"], 2)

        url = f"/api/v1/investigations/{inv['id']}/events"
        headers = {"Last-Event-ID": str(created_seq)}
        async with client.stream("GET", url, headers=headers) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            events = sse_events(resp)
            replayed = await collect(events, 2)
            assert [int(e["id"]) for e in replayed] == [e.seq for e in later]

            # Simulate the race: an already-replayed event reaches the live queue late.
            runtime(app).broker.publish(later[-1])
            live = await append(app, inv["id"], 2)
            got = await collect(events, 2)
            assert [int(e["id"]) for e in got] == [e.seq for e in live]
            ev = TimelineEvent.model_validate(json.loads(got[0]["data"]))
            assert got[0]["event"] == ev.type.value == "llm.replayed"
            assert ev.seq == live[0].seq


async def test_no_last_event_id_replays_everything_and_concurrent_writes_have_no_gap(
    make_app,
) -> None:
    app = make_app(FakeContextService())
    async with served(app) as base, httpx.AsyncClient(base_url=base, timeout=10) as client:
        inv = (await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO})).json()
        await append(app, inv["id"], 5)
        writer = asyncio.create_task(append(app, inv["id"], 20))  # races with connect/replay
        async with client.stream("GET", f"/api/v1/investigations/{inv['id']}/events") as resp:
            got = await collect(sse_events(resp), 26)
        await writer
        seqs = [int(e["id"]) for e in got]
        assert seqs == sorted(set(seqs))  # strictly increasing: no duplicate
        all_seqs = [
            e["seq"]
            for e in (
                await client.get(
                    f"/api/v1/investigations/{inv['id']}/timeline", params={"limit": 100}
                )
            ).json()["items"]
        ]
        assert seqs == all_seqs  # no gap


async def test_heartbeat_and_unsubscribe_on_disconnect(make_app) -> None:
    app = make_app(FakeContextService(), sse_heartbeat_seconds=0.1)
    rt = runtime(app)
    async with served(app) as base, httpx.AsyncClient(base_url=base, timeout=10) as client:
        inv = (await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO})).json()
        async with client.stream("GET", f"/api/v1/investigations/{inv['id']}/events") as resp:

            async def until_ping() -> None:
                async for ev in sse_events(resp):
                    if "comment" in ev:
                        return

            await asyncio.wait_for(until_ping(), 5)
            assert rt.broker.subscriber_count(inv["id"]) == 1
        for _ in range(100):
            if rt.broker.subscriber_count(inv["id"]) == 0:
                break
            await asyncio.sleep(0.02)
        assert rt.broker.subscriber_count(inv["id"]) == 0


async def test_unknown_investigation_and_bad_last_event_id(make_app) -> None:
    app = make_app(FakeContextService())
    async with served(app) as base, httpx.AsyncClient(base_url=base, timeout=10) as client:
        resp = await client.get("/api/v1/investigations/inv_NOPE/events")
        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/problem+json"
        inv = (await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO})).json()
        bad = await client.get(
            f"/api/v1/investigations/{inv['id']}/events", headers={"Last-Event-ID": "abc"}
        )
        assert bad.status_code == 422 and bad.json()["code"] == "schema_invalid"


async def test_shutdown_closes_open_streams(make_app) -> None:
    app = make_app(FakeContextService())
    rt = runtime(app)
    async with httpx.AsyncClient(timeout=10) as client, served(app) as base:
        inv = (
            await client.post(f"{base}/api/v1/investigations", json={"scenario_id": SCENARIO})
        ).json()
        stream_cm = client.stream("GET", f"{base}/api/v1/investigations/{inv['id']}/events")
        resp = await stream_cm.__aenter__()
        events = sse_events(resp)
        await collect(events, 1)
        rt.broker.close()  # what the lifespan does on SIGTERM
        remaining = [e async for e in events if "comment" not in e]
        assert remaining == []  # stream ended cleanly
        await stream_cm.__aexit__(None, None, None)


async def test_graceful_shutdown_changes_no_state(make_app) -> None:
    fake = FakeContextService()
    app = make_app(fake)
    async with served(app) as base, httpx.AsyncClient(base_url=base, timeout=10) as client:
        inv = (await client.post("/api/v1/investigations", json={"scenario_id": SCENARIO})).json()
        before = (await client.get(f"/api/v1/investigations/{inv['id']}")).json()
    app2 = make_app(fake)
    async with served(app2) as base, httpx.AsyncClient(base_url=base, timeout=10) as client:
        after = (await client.get(f"/api/v1/investigations/{inv['id']}")).json()
        timeline = (await client.get(f"/api/v1/investigations/{inv['id']}/timeline")).json()
    assert after == before
    assert len(timeline["items"]) == 1
