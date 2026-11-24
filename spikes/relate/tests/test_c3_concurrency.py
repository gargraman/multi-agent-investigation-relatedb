"""C3: 50 concurrent HTTP requests through the single write lane; concurrent-read safety."""

import asyncio
import importlib
import os
import socket
import threading
import time

import httpx
import pytest
import relate
import uvicorn

from common import bootstrap
from writelane import WriteLane


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_50_concurrent_http_writes_through_lane(store):
    os.environ["SPIKE_STORE"] = store
    import app as app_mod

    importlib.reload(app_mod)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app_mod.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)

    async def fire():
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as cl:
            writes = [cl.post("/findings", json={"id": f"f{i}"}) for i in range(50)]
            dups = [cl.post("/findings", json={"id": "dup"}) for _ in range(10)]
            reads = [cl.get("/findings/count") for _ in range(20)]
            t0 = time.perf_counter()
            res = await asyncio.wait_for(asyncio.gather(*writes, *dups, *reads), timeout=60)
            return res, time.perf_counter() - t0

    res, elapsed = asyncio.run(fire())
    w, d, r = res[:50], res[50:60], res[60:]
    print(f"\n80 concurrent requests in {elapsed:.3f}s; write statuses={sorted({x.status_code for x in w})}; "
          f"dup statuses={sorted(x.status_code for x in d)}; read statuses={sorted({x.status_code for x in r})}")
    print("lane thread ids:", app_mod.lane.thread_ids)
    assert all(x.status_code == 200 for x in w)
    assert sorted(x.status_code for x in d) == [200] + [409] * 9
    assert all(x.status_code == 200 for x in r)
    counters = sorted(x.json()["counter"] for x in w + [y for y in d if y.status_code == 200])
    assert counters == list(range(1, 52)), "lost update on shared counter"
    assert len(app_mod.lane.thread_ids) == 1

    server.should_exit = True
    t.join(timeout=10)
    # Reopen from disk and verify every acknowledged write
    conn = relate.connect(store)
    ids = {r["id"] for r in conn.execute("MATCH (f:Finding) RETURN f.id AS id").fetchall()}
    audits = conn.execute("MATCH (:AuditEvent)-[:CONCERNS]->(:Finding) RETURN count(*) AS n").single().n
    counter = conn.execute("MATCH (i:Investigation {id:'inv1'}) RETURN i.counter AS n").single().n
    print(f"after reopen: findings={len(ids)} audits={audits} counter={counter}")
    assert ids == {f"f{i}" for i in range(50)} | {"dup"}
    assert audits == 51 and counter == 51
    conn.close()


def test_shared_connection_from_other_thread_is_refused(store):
    conn = relate.connect(store)
    conn.execute("CREATE (:X)")
    err = {}

    def other():
        try:
            conn.execute("MATCH (n) RETURN count(n) AS n").fetchall()
        except Exception as e:  # noqa: BLE001
            err["e"] = e

    th = threading.Thread(target=other)
    th.start()
    th.join()
    print(f"\nforeign-thread use: {type(err['e']).__name__}: {err['e']}")
    assert isinstance(err["e"], relate.ProgrammingError)
    assert conn.execute("MATCH (n) RETURN count(n) AS n").single().n == 1  # owner still fine
    conn.close()


def test_second_writable_open_refused_same_process(store):
    conn = relate.connect(store)
    with pytest.raises(relate.Error) as ei:
        relate.connect(store)
    print(f"\nsecond writable open: {type(ei.value).__name__}: {ei.value}")
    conn.close()


def test_read_only_handles_beside_writer(store):
    """Can other threads read through their own read_only handles while the lane writes?
    And do they see new commits (snapshot vs live freshness)?"""
    lane = WriteLane(store, bootstrap)
    lane.submit_sync(lambda c: c.execute("CREATE (:Finding {id:'seed'})"))
    ro = relate.connect(store, read_only=True)
    before = ro.execute("MATCH (f:Finding) RETURN count(f) AS n").single().n
    lane.submit_sync(lambda c: c.execute("CREATE (:Finding {id:'after-ro-open'})"))
    after = ro.execute("MATCH (f:Finding) RETURN count(f) AS n").single().n
    print(f"\nread_only handle: count at open={before}, after a later lane commit={after} "
          f"({'LIVE' if after == 2 else 'SNAPSHOT-at-open'})")
    with pytest.raises(relate.Error) as ei:
        ro.execute("CREATE (:Finding {id:'ro-write'})")
    print(f"write via read_only: {type(ei.value).__name__}: {ei.value}")
    ro.close()

    # 8 reader threads, each with its own read_only handle, hammering while the lane writes
    errors, reads = [], [0]
    stop = threading.Event()

    def reader():
        try:
            h = relate.connect(store, read_only=True)
            while not stop.is_set():
                h.execute("MATCH (f:Finding) RETURN count(f) AS n").single()
                reads[0] += 1
            h.close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    ths = [threading.Thread(target=reader) for _ in range(8)]
    for th in ths:
        th.start()
    t0 = time.perf_counter()
    for i in range(200):
        lane.submit_sync(lambda c, i=i: c.execute("CREATE (:Finding {id:$id})", {"id": f"w{i}"}))
    wdur = time.perf_counter() - t0
    stop.set()
    for th in ths:
        th.join()
    final = lane.submit_sync(lambda c: c.execute("MATCH (f:Finding) RETURN count(f) AS n").single().n, read=True)
    print(f"8 RO reader threads: reads={reads[0]} errors={errors[:3]}; 200 lane writes in {wdur:.3f}s; final={final}")
    assert not errors and final == 202
    lane.close()
