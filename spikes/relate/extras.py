"""Extras: memory footprint (~10k nodes), open/close time, read-only open cost,
SIGTERM behaviour. Run: uv run --project spikes/relate python spikes/relate/extras.py
"""

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import relate

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import bootstrap, cleanup, fresh_store  # noqa: E402

proc = psutil.Process()


def rss():
    return proc.memory_info().rss / 2**20


def du(path):
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) / 2**20


def memory_and_timing():
    store = fresh_store("extras")
    base = rss()
    t0 = time.perf_counter()
    conn = relate.connect(store)
    bootstrap(conn)
    t_open_empty = time.perf_counter() - t0
    n = 10_000
    rows = [{"uid": f"inv1:E{i}", "mid": f"E{i}", "summary": "x" * 100} for i in range(n)]
    t0 = time.perf_counter()
    conn.execute("UNWIND $rows AS r CREATE (:Entity {uid:r.uid, manifest_id:r.mid, investigation_id:'inv1', summary:r.summary})",
                 {"rows": rows})
    conn.execute("MATCH (a:Entity) WITH a, toInteger(substring(a.manifest_id,1)) AS i "
                 "MATCH (b:Entity {uid:'inv1:E' + toString((i+1) % 10000)}) CREATE (a)-[:RELATED_TO {type:'X'}]->(b)")
    t_load = time.perf_counter() - t0
    after_load = rss()
    t0 = time.perf_counter()
    for i in range(200):
        conn.execute("MATCH (e:Entity {uid:$u})-[:RELATED_TO*1..3]->(x) RETURN count(x)", {"u": f"inv1:E{i}"}).fetchall()
    t_q = (time.perf_counter() - t0) / 200
    wal_mb = du(store)
    t0 = time.perf_counter()
    conn.close()
    t_close = time.perf_counter() - t0
    disk_after_close = du(store)
    t0 = time.perf_counter()
    conn = relate.connect(store)
    nn = conn.execute("MATCH (n) RETURN count(n) AS n").single().n
    t_reopen = time.perf_counter() - t0
    conn.close()
    t0 = time.perf_counter()
    for _ in range(20):
        ro = relate.connect(store, read_only=True)
        ro.execute("MATCH (n) RETURN count(n) AS n").single()
        ro.close()
    t_ro = (time.perf_counter() - t0) / 20
    print(f"10k nodes + 10k rels: load={t_load*1000:.0f}ms RSS base={base:.0f}MiB after_load={after_load:.0f}MiB "
          f"(+{after_load-base:.0f}MiB); 3-hop query avg={t_q*1000:.2f}ms")
    print(f"open(empty)+bootstrap={t_open_empty*1000:.1f}ms close(checkpoint)={t_close*1000:.0f}ms "
          f"reopen+count={t_reopen*1000:.0f}ms (n={nn}); read_only open+query+close avg={t_ro*1000:.1f}ms")
    print(f"disk before close={wal_mb:.1f}MiB after close={disk_after_close:.1f}MiB; files: "
          f"{sorted(str(p.relative_to(store)) for p in Path(store).rglob('*'))[:12]}")
    cleanup(store)


SIGTERM_WRITER = textwrap.dedent("""
    import os, sys, signal, relate
    store, mode = sys.argv[1], sys.argv[2]
    conn = relate.connect(store)
    if mode == 'graceful':
        def h(*_):
            conn.close(); print('CLOSED', flush=True); sys.exit(0)
        signal.signal(signal.SIGTERM, h)
    i = 0
    print('READY', flush=True)
    while True:
        conn.execute('CREATE (:T {i:$i})', {'i': i}); i += 1
        if i % 100 == 0:
            print(i, flush=True)
""")


def sigterm(mode):
    store = fresh_store("sigterm")
    p = subprocess.Popen([sys.executable, "-c", SIGTERM_WRITER, store, mode], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "READY"
    last = 0
    while last < 500:
        line = p.stdout.readline().strip()
        if line.isdigit():
            last = int(line)
    p.send_signal(signal.SIGTERM)
    out = p.communicate(timeout=20)[0]
    lines = [l for l in out.split() if l.isdigit()]
    acked_floor = int(lines[-1]) if lines else last
    conn = relate.connect(store)
    n = conn.execute("MATCH (t:T) RETURN count(t) AS n").single().n
    conn.close()
    print(f"SIGTERM ({mode}): rc={p.returncode} 'CLOSED' printed={'CLOSED' in out}; "
          f"last printed count={acked_floor}; present after reopen={n}; lease released (reopen ok)=True")
    cleanup(store)


if __name__ == "__main__":
    print(f"relate-db {relate.__version__}, python {sys.version.split()[0]}, pid {os.getpid()}")
    memory_and_timing()
    sigterm("default")
    sigterm("graceful")
