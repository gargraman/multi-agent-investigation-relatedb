"""Shared helpers for the Relate S0 spike (ADR-001).

Schema DDL mirrors spec §9.3; the seed plants the ADR-007 I4 pattern.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import relate

SPIKE_DIR = Path(__file__).resolve().parent
STORE_ROOT = SPIKE_DIR / "store"
RELATE_CLI = Path(os.environ.get("RELATE_CLI", STORE_ROOT / "bin" / "relate-darwin-arm64"))

# --- Schema bootstrap (spec §9.3) -----------------------------------------
SCHEMA_DDL = [
    *[
        f"CREATE CONSTRAINT {lbl.lower()}_id IF NOT EXISTS FOR (n:{lbl}) REQUIRE n.id IS UNIQUE"
        for lbl in ["Finding", "Assertion", "Assessment", "Decision", "AuditEvent", "Submission", "Investigation"]
    ],
    "CREATE CONSTRAINT entity_uid IF NOT EXISTS FOR (n:Entity) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT evidence_uid IF NOT EXISTS FOR (n:Evidence) REQUIRE n.uid IS UNIQUE",
    "CREATE CONSTRAINT submission_idem IF NOT EXISTS FOR (n:Submission) REQUIRE n.idempotency_key IS UNIQUE",
    "CREATE INDEX finding_inv IF NOT EXISTS FOR (n:Finding) ON (n.investigation_id)",
    "CREATE INDEX entity_inv IF NOT EXISTS FOR (n:Entity) ON (n.investigation_id)",
    "CREATE INDEX evidence_inv IF NOT EXISTS FOR (n:Evidence) ON (n.investigation_id)",
    "CREATE INDEX entity_manifest IF NOT EXISTS FOR (n:Entity) ON (n.manifest_id)",
    "CREATE INDEX assertion_type IF NOT EXISTS FOR (n:Assertion) ON (n.type)",
    "CREATE POINT INDEX location_position IF NOT EXISTS FOR (n:Location) ON (n.position)",
]


def bootstrap(conn: relate.Connection) -> None:
    for ddl in SCHEMA_DDL:
        conn.execute(ddl)


def fresh_store(name: str = "s") -> str:
    """A new, empty store path under spikes/relate/store/tmp/ (git-ignored)."""
    root = STORE_ROOT / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=root))
    return str(d / "db")


def cleanup(path: str) -> None:
    shutil.rmtree(Path(path).parent, ignore_errors=True)


def dt(h: int, m: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


# --- Seed: the I4 pattern (ADR-007) plus I1/I2/I3 fixtures -----------------
PITCH = 1.0
RADIUS = 1.5 * PITCH


def cell_center(col: int, row: int) -> relate.Point:
    # srid 7203 = cartesian 2D
    return relate.Point(7203, col * PITCH + PITCH / 2, row * PITCH + PITCH / 2)


SEED_CYPHER = """
MERGE (inv:Investigation {id: $inv})
WITH inv
UNWIND $entities AS e
CREATE (n:Entity {uid: $inv + ':' + e.mid, manifest_id: e.mid, investigation_id: $inv, type: e.type, status: 'RESOLVED'})
CREATE (inv)-[:HAS_ENTITY]->(n)
"""


def seed_investigation(conn: relate.Connection, inv: str) -> None:
    """Seed one investigation as ONE transaction. Caller owns autocommit mode."""
    entities = [
        {"mid": m, "type": t}
        for m, t in [
            ("A", "PERSON"), ("B", "PERSON"), ("C", "PERSON"), ("E", "PERSON"),
            ("hA", "HANDLE"), ("hC", "HANDLE"), ("hB", "HANDLE"),
        ]
    ]
    conn.execute(SEED_CYPHER, {"inv": inv, "entities": entities})
    # Location cells (secondary label) with cartesian points
    cells = {"cell_2_2": (2, 2), "cell_3_3": (3, 3), "cell_8_8": (8, 8)}
    for mid, (c, r) in cells.items():
        conn.execute(
            "MATCH (inv:Investigation {id:$inv}) "
            "CREATE (l:Entity:Location {uid:$inv+':'+$mid, manifest_id:$mid, investigation_id:$inv, "
            "type:'LOCATION', position:$pos}) CREATE (inv)-[:HAS_ENTITY]->(l)",
            {"inv": inv, "mid": mid, "pos": cell_center(c, r)},
        )
    # Manifest RELATED_TO (handle USED_BY person)
    for h, p in [("hA", "A"), ("hC", "C"), ("hB", "B")]:
        conn.execute(
            "MATCH (h:Entity {uid:$h}), (p:Entity {uid:$p}) CREATE (h)-[:RELATED_TO {type:'USED_BY'}]->(p)",
            {"h": f"{inv}:{h}", "p": f"{inv}:{p}"},
        )
    # Evidence
    for ev in ["ev_sig", "ev_geo_c", "ev_geo_b"]:
        conn.execute(
            "MATCH (inv:Investigation {id:$inv}) CREATE (e:Evidence {uid:$inv+':'+$ev, manifest_id:$ev, "
            "investigation_id:$inv, status:'ACTIVE', sha256:'00'}) CREATE (inv)-[:HAS_EVIDENCE]->(e)",
            {"inv": inv, "ev": ev},
        )

    def finding(fid, agent, ev, about, rank=1, status="ACTIVE"):
        conn.execute(
            "MATCH (inv:Investigation {id:$inv}), (e:Evidence {uid:$ev}) "
            "CREATE (f:Finding {id:$fid, investigation_id:$inv, source_agent_id:$agent, status:$status, "
            "classification_rank:$rank, valid_from:$vf, valid_until:$vu}) "
            "CREATE (inv)-[:CONTAINS]->(f) CREATE (f)-[:SUPPORTED_BY]->(e) "
            "WITH f UNWIND $about AS a MATCH (x:Entity {uid:a}) CREATE (f)-[:ABOUT]->(x)",
            {"inv": inv, "fid": fid, "agent": agent, "ev": f"{inv}:{ev}", "status": status, "rank": rank,
             "vf": dt(0), "vu": dt(23), "about": [f"{inv}:{a}" for a in about]},
        )

    # SIGINT: hA <-> hC contact (canonical: lower manifest_id is SUBJECT -> 'hA' < 'hC')
    finding(f"{inv}:fs", "sigint-1", "ev_sig", ["hA", "hC"])
    conn.execute(
        "MATCH (f:Finding {id:$f}), (s:Entity {uid:$s}), (o:Entity {uid:$o}) "
        "CREATE (f)-[:ASSERTS]->(a:Assertion {id:$aid, type:'LINKED_TO', link_type:'CONTACT', valid_from:$vf, valid_until:$vu}) "
        "CREATE (a)-[:SUBJECT]->(s) CREATE (a)-[:OBJECT]->(o)",
        {"f": f"{inv}:fs", "s": f"{inv}:hA", "o": f"{inv}:hC", "aid": f"{inv}:l1", "vf": dt(9), "vu": dt(10)},
    )

    def placed(fid, aid, who, cell, vf, vu):
        conn.execute(
            "MATCH (f:Finding {id:$f}), (s:Entity {uid:$s}), (l:Location {uid:$l}) "
            "CREATE (f)-[:ASSERTS]->(a:Assertion {id:$aid, type:'PLACED_AT', valid_from:$vf, valid_until:$vu}) "
            "CREATE (a)-[:SUBJECT]->(s) CREATE (a)-[:AT]->(l)",
            {"f": fid, "s": f"{inv}:{who}", "l": f"{inv}:{cell}", "aid": aid, "vf": vf, "vu": vu},
        )

    # GEOINT: C at cell_2_2 [10:00,12:00); B at cell_3_3 (diagonal neighbour) [11:00,13:00)
    finding(f"{inv}:fc", "geoint-1", "ev_geo_c", ["C"])
    placed(f"{inv}:fc", f"{inv}:pc", "C", "cell_2_2", dt(10), dt(12))
    finding(f"{inv}:fb", "geoint-1", "ev_geo_b", ["B", "E"])
    placed(f"{inv}:fb", f"{inv}:pb", "B", "cell_3_3", dt(11), dt(13))
    # Distractors: E far away (overlapping time), E near C but non-overlapping time
    placed(f"{inv}:fb", f"{inv}:pe1", "E", "cell_8_8", dt(10), dt(12))
    placed(f"{inv}:fb", f"{inv}:pe2", "E", "cell_3_3", dt(14), dt(15))

    # I1: assessment over fs, fc
    conn.execute(
        "MATCH (inv:Investigation {id:$inv}), (f1:Finding {id:$f1}), (f2:Finding {id:$f2}) "
        "CREATE (a:Assessment {id:$aid, investigation_id:$inv, classification_rank:1}) "
        "CREATE (a)-[:BELONGS_TO]->(inv) CREATE (a)-[:USES_FINDING]->(f1) CREATE (a)-[:USES_FINDING]->(f2)",
        {"inv": inv, "aid": f"{inv}:as1", "f1": f"{inv}:fs", "f2": f"{inv}:fc"},
    )
    # I2: inference chain fs <- g1 <- g2 (DERIVED_FROM), assessment as2 uses g2
    for gid, src in [("g1", "fs"), ("g2", "g1")]:
        conn.execute(
            "MATCH (inv:Investigation {id:$inv}), (s:Finding {id:$src}) "
            "CREATE (g:Finding {id:$gid, investigation_id:$inv, source_agent_id:'analysis', status:'ACTIVE', "
            "classification_rank:1, kind:'INFERENCE'}) CREATE (inv)-[:CONTAINS]->(g) CREATE (g)-[:DERIVED_FROM]->(s)",
            {"inv": inv, "gid": f"{inv}:{gid}", "src": f"{inv}:{src}"},
        )
    conn.execute(
        "MATCH (inv:Investigation {id:$inv}), (g:Finding {id:$g}) "
        "CREATE (a:Assessment {id:$aid, investigation_id:$inv, classification_rank:1}) "
        "CREATE (a)-[:BELONGS_TO]->(inv) CREATE (a)-[:USES_FINDING]->(g)",
        {"inv": inv, "aid": f"{inv}:as2", "g": f"{inv}:g2"},
    )
    # I3: two findings about C that contradict
    for fid in ["k1", "k2"]:
        conn.execute(
            "MATCH (inv:Investigation {id:$inv}), (c:Entity {uid:$c}) "
            "CREATE (f:Finding {id:$fid, investigation_id:$inv, source_agent_id:'x', status:'DISPUTED', classification_rank:1}) "
            "CREATE (inv)-[:CONTAINS]->(f) CREATE (f)-[:ABOUT]->(c)",
            {"inv": inv, "fid": f"{inv}:{fid}", "c": f"{inv}:C"},
        )
    conn.execute(
        "MATCH (a:Finding {id:$a}), (b:Finding {id:$b}) CREATE (a)-[:CONTRADICTS]->(b)",
        {"a": f"{inv}:k1", "b": f"{inv}:k2"},
    )


def open_seeded(path: str, invs=("inv1",)) -> relate.Connection:
    conn = relate.connect(path)
    bootstrap(conn)
    for inv in invs:
        conn.autocommit = False
        seed_investigation(conn, inv)
        conn.commit()
        conn.autocommit = True
    return conn
