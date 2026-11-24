"""Minimal FastAPI service with the Relate write lane (for C3)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import relate
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from common import bootstrap
from writelane import WriteLane

STORE = os.environ.get("SPIKE_STORE", "")
lane: WriteLane | None = None


@asynccontextmanager
async def lifespan(_app):
    global lane
    lane = WriteLane(STORE, bootstrap)
    lane.submit_sync(lambda c: c.execute("MERGE (i:Investigation {id:'inv1'}) ON CREATE SET i.counter = 0"))
    yield
    lane.close()


app = FastAPI(lifespan=lifespan)


class FindingIn(BaseModel):
    id: str


@app.post("/findings")
async def submit_finding(body: FindingIn):
    def op(conn: relate.Connection):
        # one domain op = one tx: finding + audit + read-modify-write on a shared node
        conn.execute(
            "MATCH (i:Investigation {id:'inv1'}) CREATE (i)-[:CONTAINS]->(f:Finding {id:$id, status:'ACTIVE'})",
            {"id": body.id},
        )
        conn.execute(
            "MATCH (f:Finding {id:$id}) CREATE (:AuditEvent {id:'audit-'+$id, op:'submit'})-[:CONCERNS]->(f)",
            {"id": body.id},
        )
        n = conn.execute(
            "MATCH (i:Investigation {id:'inv1'}) SET i.counter = i.counter + 1 RETURN i.counter AS n"
        ).single().n
        return n

    try:
        n = await lane.write(op)
    except relate.IntegrityError as e:
        raise HTTPException(409, str(e))
    return {"id": body.id, "counter": n}


@app.get("/findings/count")
async def count():
    def op(conn):
        return conn.execute("MATCH (f:Finding) RETURN count(f) AS n").single().n

    return {"n": await lane.read(op)}
