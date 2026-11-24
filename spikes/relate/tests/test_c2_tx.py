"""C2: transaction API — multi-statement commit and rollback."""

import relate
import pytest

from common import bootstrap


def count(conn, label):
    return conn.execute(f"MATCH (n:{label}) RETURN count(n) AS n").single().n


def test_multi_statement_commit_survives_reopen(store):
    conn = relate.connect(store)
    bootstrap(conn)
    conn.autocommit = False
    conn.execute("CREATE (:Finding {id:'f1'})")
    conn.execute("CREATE (:AuditEvent {id:'a1'})")
    conn.execute("MATCH (f:Finding {id:'f1'}), (a:AuditEvent {id:'a1'}) CREATE (a)-[:CONCERNS]->(f)")
    # read-your-own-writes inside the tx
    assert count(conn, "Finding") == 1
    bm1 = conn.commit()
    conn.execute("CREATE (:Finding {id:'f2'})")
    bm2 = conn.commit()
    print("\nbookmarks:", bm1, bm2)
    assert bm1.startswith("relate:") and int(bm2.split(":")[1]) > int(bm1.split(":")[1])
    conn.close()
    conn = relate.connect(store)
    assert count(conn, "Finding") == 2
    assert conn.execute("MATCH (:AuditEvent)-[r:CONCERNS]->(:Finding) RETURN count(r) AS n").single().n == 1
    conn.close()


def test_rollback_discards_all_statements(store):
    conn = relate.connect(store)
    bootstrap(conn)
    conn.autocommit = False
    conn.execute("CREATE (:Finding {id:'r1'})")
    conn.execute("CREATE (:AuditEvent {id:'ra1'})")
    conn.rollback()
    assert count(conn, "Finding") == 0 and count(conn, "AuditEvent") == 0
    conn.close()
    conn = relate.connect(store)
    assert count(conn, "Finding") == 0 and count(conn, "AuditEvent") == 0
    conn.close()


def test_failed_statement_mid_transaction(store):
    """Domain op = one tx: if statement 2 fails, statement 1 must not persist."""
    conn = relate.connect(store)
    bootstrap(conn)
    conn.execute("CREATE (:Finding {id:'dup'})")
    conn.autocommit = False
    conn.execute("CREATE (:Finding {id:'ok-1'})")
    with pytest.raises(relate.IntegrityError):
        conn.execute("CREATE (:Finding {id:'dup'})")
    # What does commit do after a failed statement? Record the behaviour.
    try:
        bm = conn.commit()
        outcome = f"commit accepted, bookmark={bm}"
    except relate.Error as e:
        outcome = f"commit refused: {type(e).__name__}: {e}"
        conn.rollback()
    present = conn.execute("MATCH (f:Finding {id:'ok-1'}) RETURN count(f) AS n").single().n
    print(f"\nafter failed stmt: {outcome}; ok-1 present={present}")
    assert outcome.startswith("commit refused") and "rollback required" in outcome
    assert present == 0
    conn.autocommit = True
    conn.close()


def test_close_with_open_tx_rolls_back(store):
    conn = relate.connect(store)
    conn.autocommit = False
    conn.execute("CREATE (:Finding {id:'never'})")
    conn.close()
    conn = relate.connect(store)
    assert count(conn, "Finding") == 0
    conn.close()
