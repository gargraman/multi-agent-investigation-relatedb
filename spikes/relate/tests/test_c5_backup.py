"""C5: `relate clone` backup produces a copy that opens and contains the data.

The clone command is NOT in the relate-db wheel; it ships in the standalone CLI
binary (release asset relate-<os>-<arch>). Path via $RELATE_CLI, default
store/bin/relate-darwin-arm64 (see README).
"""

import os
import subprocess
from pathlib import Path

import pytest
import relate

import queries as Q
from common import RADIUS, RELATE_CLI, open_seeded

pytestmark = pytest.mark.skipif(not Path(RELATE_CLI).exists(), reason=f"relate CLI not found at {RELATE_CLI}")


def clone(src, dst):
    r = subprocess.run([str(RELATE_CLI), "clone", src, dst], capture_output=True, text=True,
                       env={**os.environ, "DO_NOT_TRACK": "1"})  # CLI telemetry is ON by default
    print(f"\n$ relate clone {Path(src).name} {Path(dst).name} -> rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
    return r


def snapshot(conn):
    return (
        conn.execute("MATCH (n) RETURN count(n) AS n").single().n,
        conn.execute("MATCH ()-[r]->() RETURN count(r) AS n").single().n,
        len(conn.execute("SHOW CONSTRAINTS").fetchall()),
        len(conn.execute("SHOW INDEXES").fetchall()),
    )


def i4(conn):
    return [(r["entity_a"], r["via"], r["entity_b"]) for r in
            conn.execute(Q.I4, {"inv": "inv1", "max_rank": 1, "radius": RADIUS}).fetchall()]


def test_clone_beside_live_writer_and_after_close(store):
    conn = open_seeded(store)  # writer still open; commits live only in WAL (no flush)
    conn.execute("CREATE (:Finding {id:'post-seed-wal-only'})")
    expected = snapshot(conn)
    base = Path(store).parent

    # 1) directory -> directory while the writer holds the lease
    live = str(base / "clone-live")
    assert clone(store, live).returncode == 0
    # writer keeps working after the clone
    conn.execute("CREATE (:Finding {id:'after-clone'})")
    c = relate.connect(live)
    got = snapshot(c)
    print("source (at clone):", expected, "clone-live:", got)
    assert got == expected
    assert c.execute("MATCH (f:Finding {id:'post-seed-wal-only'}) RETURN count(f) AS n").single().n == 1
    assert c.execute("MATCH (f:Finding {id:'after-clone'}) RETURN count(f) AS n").single().n == 0
    assert i4(c) == [("A", "C", "B")]
    # constraints are enforced on the restored copy
    with pytest.raises(relate.IntegrityError):
        c.execute("CREATE (:Entity {uid:'inv1:A'})")
    c.close()

    # 2) single-file bundle (.rl) and restore from it, after a graceful close
    conn.close()
    bundle = str(base / "backup.rl")
    restored = str(base / "restored")
    assert clone(store, bundle).returncode == 0
    assert clone(bundle, restored).returncode == 0
    c = relate.connect(restored)
    got2 = snapshot(c)
    print("restored-from-.rl:", got2)
    assert got2[0] == expected[0] + 1 and i4(c) == [("A", "C", "B")]
    c.close()

    # 3) destination must not exist
    r = clone(store, live)
    assert r.returncode == 1
