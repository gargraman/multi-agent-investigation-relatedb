"""C4: kill -9 during writes; every acknowledged transaction must be present after reopen."""

import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import relate

SPIKE = Path(__file__).resolve().parent.parent
ROUNDS = int(os.environ.get("C4_ROUNDS", "5"))


def _run_round(store, ackfile, start_seq, batch, min_acks):
    p = subprocess.Popen([sys.executable, str(SPIKE / "writer_proc.py"), store, str(ackfile), str(start_seq), str(batch)],
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "READY"
    deadline = time.time() + 60
    while time.time() < deadline:
        if ackfile.exists() and len(ackfile.read_text().splitlines()) >= min_acks:
            break
        time.sleep(0.005)
    time.sleep(random.uniform(0, 0.05))  # land the kill at a random point in the stream
    os.kill(p.pid, signal.SIGKILL)
    p.wait()
    return p.returncode


def test_kill9_acknowledged_commits_survive(store, tmp_path):
    ackfile = tmp_path / "acks.txt"
    seq = 0
    batch = 20
    known_unacked: set[str] = set()  # committed-but-unacked from earlier rounds (allowed, at most 1 per kill)
    for rnd in range(ROUNDS):
        rc = _run_round(store, ackfile, seq, batch, min_acks=seq + random.randint(20, 200))
        acked = ackfile.read_text().splitlines()
        t0 = time.perf_counter()
        conn = relate.connect(store)  # reopen: snapshot + WAL replay
        reopen = time.perf_counter() - t0
        rows = conn.execute(
            "MATCH (f:Finding) OPTIONAL MATCH (f)-[:ASSERTS]->(a) WITH f, count(a) AS na "
            "OPTIONAL MATCH (e:AuditEvent)-[:CONCERNS]->(f) RETURN f.id AS id, na, count(e) AS ne"
        ).fetchall()
        present = {r["id"]: (r["na"], r["ne"]) for r in rows}
        missing = [a for a in acked if a not in present]
        torn = {k: v for k, v in present.items() if v != (batch, 1)}
        extra = sorted(set(present) - set(acked) - known_unacked)
        known_unacked |= set(extra)
        print(f"\nround {rnd}: rc={rc} acked={len(acked)} present={len(present)} missing={len(missing)} "
              f"torn={len(torn)} committed-but-unacked={extra} reopen={reopen*1000:.1f}ms")
        assert not missing, f"acknowledged commits lost: {missing[:5]}"
        assert not torn, f"partially applied transactions: {list(torn.items())[:5]}"
        assert len(extra) <= 1
        seq = max(int(x[2:]) for x in present) + 1
        conn.close()
