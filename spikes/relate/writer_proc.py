"""C4 writer subprocess: commits transactions forever and logs each one to an
ack file ONLY after commit() returns (fsync'd). Usage:
    python writer_proc.py STORE ACKFILE START_SEQ BATCH
Each tx creates one Finding {id: seq} + BATCH Assertion children + one AuditEvent.
"""
import os
import sys

import relate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import bootstrap  # noqa: E402

store, ackfile, seq, batch = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
conn = relate.connect(store)
bootstrap(conn)
conn.autocommit = False
ack = open(ackfile, "a")
print("READY", flush=True)
while True:
    conn.execute(
        "CREATE (f:Finding {id:$id, n:$batch}) WITH f UNWIND range(1,$batch) AS k "
        "CREATE (f)-[:ASSERTS]->(:Assertion {id:$id + '-' + toString(k)})",
        {"id": f"tx{seq}", "batch": batch},
    )
    conn.execute("MATCH (f:Finding {id:$id}) CREATE (:AuditEvent {id:'a'+$id})-[:CONCERNS]->(f)", {"id": f"tx{seq}"})
    conn.commit()
    ack.write(f"tx{seq}\n")
    ack.flush()
    os.fsync(ack.fileno())
    seq += 1
