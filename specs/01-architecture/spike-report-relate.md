# S0 spike report: Relate embedded (ADR-001)

- **Date:** 2026-11-24
- **Scope:** ADR-001 exit criteria 1–7, plus extras (memory, open/close time, SIGTERM)
- **Code:** `spikes/relate/` (standalone uv project; re-run steps in `spikes/relate/README.md`)
- **Recommendation:** **GO-WITH-CONDITIONS.** Keep ADR-001. The conditions are listed in §7.

## TL;DR

Every ADR-001 exit criterion passed on macOS arm64. The ADR-007 I4 query runs **verbatim**: it returns exactly `{A, C, B}`, and the path disappears in both negative cases.

Several spec assumptions are wrong in the details. None of them blocks the decision:
- "single user/agent per process" is stricter than it sounds;
- `relate clone` is not in the Python package;
- the CLI phones home by default;
- the point index is created but never used by the planner;
- read-only handles are frozen snapshots.

The main residual risk is maturity: five releases in two weeks, and an "Alpha" classifier.

## 1. Environment

| Item | Value |
|---|---|
| OS / arch | macOS 27.2.1 (Darwin 27.2.0), arm64 (Apple Silicon) |
| Python | 3.12.13 (uv 0.13.6; spike venv at `spikes/relate/.venv`) |
| relate-db | **1.2.0** (`relate_db-1.2.0-cp39-abi3-macosx_11_0_arm64.whl`). This is the latest on PyPI; the versions are 1.0.0, 1.0.1, 1.0.2, 1.1.0 and 1.2.0, all from the last ~2 weeks. Classifier: `Development Status :: 3 - Alpha` |
| relate CLI | v1.2.0, GitHub release asset `relate-darwin-arm64`, sha256 `347511ff…5f123c90` (matches `SHA256SUMS`) |
| Other spike deps | fastapi, uvicorn 0.54, httpx, pytest 9.2, psutil (versions in `spikes/relate/uv.lock`) |
| Not tested | Linux / Docker. Wheels exist for linux x86_64 and aarch64, but nothing ran in a container |

## 2. The real API, as discovered

Sources: the wheel's `relate/*.py` and native docstrings, the PyPI README, and GitHub `docs/limitations.md`, `docs/reference/python-library.md` and `docs/api/cli/spec.md`.

### Python (DB-API 2.0 / PEP 249 style; `import relate`)

```python
conn = relate.connect(path, namespace="neo4j", read_only=False)  # path: dir | "x.rl" | ":memory:"
cur  = conn.execute(cypher, {"param": value})     # $param placeholders; params must be a dict
rows = cur.fetchall()                             # Record = dict + attribute access; .single(), .data(), .consume()
conn.autocommit = False                           # default True (each statement durable on return)
conn.execute(...); conn.execute(...)
bookmark = conn.commit()                          # "relate:<n>", monotonic; or conn.rollback()
conn.flush()                                      # checkpoint WAL -> Parquet snapshot
conn.close()                                      # rolls back an open tx, then checkpoints
```

- **No `with conn.transaction():` or `begin()` in the Python wrapper.** Transactions are only `autocommit=False` plus `commit()`/`rollback()`. (The native `relate.Database` has `begin_transaction`/`commit_transaction`/`rollback_transaction`.)
- **Errors** follow the PEP 249 tree and carry `neo4j_code`, `gql_status` and `retryable`. A unique violation raises `relate.IntegrityError` with code `Neo.ClientError.Schema.ConstraintValidationFailed`.
- **Value types:**
  - Points: pass `relate.Point(7203, x, y)` (srid 7203 is cartesian 2D), or use `point({x:..,y:..})` in Cypher. A plain dict is **not** turned into a point.
  - `datetime` with a tz round-trips as Cypher `DATETIME`.
- **`threadsafety = 1`:** a connection is **thread-affine**. Using it from another thread raises `ProgrammingError` (observed).
- `relate.connect(path)` on a store that is already open **writable** fails with `OperationalError: storage locked: …/.cypherlite.writer`. This happens **even in the same process**.
- `read_only=True` is a separate handle allowed beside a writer, but it is a **snapshot taken at open** (observed). The engine has a "live" WAL-follow mode, but it is only exposed on the CLI (`--read-only=live`), not in Python 1.2.0.
- Durability knobs (env vars found in the native lib): `RELATE_DURABILITY_MODE` (`full` is the default; `everysecond` and `snapshot` accept data loss) and `RELATE_CHECKPOINT_INTERVAL_SECS`.

### DDL (Neo4j 5 syntax, accepted verbatim)

```cypher
CREATE CONSTRAINT entity_uid IF NOT EXISTS FOR (n:Entity) REQUIRE n.uid IS UNIQUE
CREATE INDEX entity_manifest IF NOT EXISTS FOR (n:Entity) ON (n.manifest_id)
CREATE POINT INDEX location_position IF NOT EXISTS FOR (n:Location) ON (n.position)
SHOW CONSTRAINTS / SHOW INDEXES
```

All of these are idempotent with `IF NOT EXISTS`. A unique constraint creates an owned RANGE index. The full bootstrap list (10 constraints, 5 range indexes, 1 point index) is `SCHEMA_DDL` in `spikes/relate/common.py`.

### Backup

`relate clone SOURCE DEST` (exit 0, silent on success; DEST must not exist). Format is inferred from the path: a `.rl` suffix means a single-file bundle, anything else a native directory.

## 3. Results

Command for all rows: `uv run --project spikes/relate pytest spikes/relate/tests -s -q`. Result: **46 passed** (C4 with `C4_ROUNDS=20`), 14.6 s.

| # | Criterion | Result | Evidence (test → key output) | Notes / workaround |
|---|---|---|---|---|
| 1 | Constraint, index and point-index DDL | **PARTIAL** (DDL PASS; point index unused) | `test_c1_schema.py`: bootstrap ran twice with no error. `SHOW CONSTRAINTS` lists 10 × `NODE_PROPERTY_UNIQUENESS`, and `location_position` is type `POINT`, `ONLINE`. Both survive reopen. Duplicate `Entity.uid` → `IntegrityError … ConstraintValidationFailed: entity_uid uniqueness`, also rejected for an `:Entity:Location` node, inside an explicit tx, and on `Submission.idempotency_key`. `MERGE` is idempotent. Stored point = `Point(srid=7203, x=2.5, y=2.5) crs cartesian`. Distance ≤ 1.5 returns the cell plus its 8 neighbours. | **The point index is never used.** `PROFILE` with 3,600 Locations shows `NodeByLabelScan … WHERE filter` for both `point.distance(...) <= r` and `point.withinBBox`. Results are correct. Irrelevant for I4, which reaches Locations by traversal; matters only for a future "near point" lookup at scale |
| 2 | Multi-statement commit / rollback | **PASS** | `test_c2_tx.py`: 3-statement tx committed (bookmarks `relate:17` → `relate:18`, monotonic) and visible after reopen. Rollback of 2 statements: 0 nodes, before and after reopen. Close with an open tx rolls it back. | **Poisoned tx:** after a failed statement, `commit()` raises `OperationalError: transaction failed; rollback required`, and the earlier statement's write is absent. GraphRepository must always `rollback()` on exception (the prototype lane does) |
| 3 | 50 concurrent HTTP requests via the write lane | **PASS** | `test_c3_concurrency.py`: FastAPI + uvicorn + httpx sent 50 writes + 10 same-id duplicates + 20 reads concurrently, all done in **0.123 s**. Writes all `200`; duplicates exactly `[200, 409×9]`; reads `200`. The shared counter (read-modify-write in each tx) took values **1..51 with no gaps**, so no lost updates. One lane thread id. After reopen: findings=51, audits=51, counter=51. | Lane = `ThreadPoolExecutor(max_workers=1)` that owns the connection; `asyncio.wrap_future` (`spikes/relate/writelane.py`) |
| 3b | Concurrent reads from other threads | **PASS with caveat** | Shared connection from another thread → `ProgrammingError: …connections are thread-affine…`. A second writable open → `storage locked`. 8 threads, each with its own `read_only=True` handle, made 4,433 reads during 200 lane writes: 0 errors, final count correct. | **Read-only handles do not see commits made after they open** (count stayed 1 after a lane commit), so they can't serve fresh reads. Options: (a) reads share the lane (recommended; reads are sub-ms at demo scale); (b) open a fresh RO handle per request (~25 ms at 10k nodes) |
| 4 | `kill -9` mid-stream; every acked commit present | **PASS** | `test_c4_durability.py`, 20 rounds on one store: 3,306 acked transactions, each with 1 Finding + 20 Assertions + 1 AuditEvent. **missing=0, torn=0 in every round.** Up to one committed-but-unacked tx per kill (expected). Reopen after kill: 9 → 146 ms. | Tests process crash only, not OS crash or power loss (macOS fsync semantics not verified). Stale writer lease after `kill -9` was reclaimed automatically. Reopen time grows with WAL length when nothing checkpoints: call `flush()` periodically |
| 5 | Backup restores correctly | **PASS** | `test_c5_backup.py`: `relate clone db clone-live` **while the writer was open** → rc=0. The clone matched the source at clone time (29 nodes / 56 rels / 10 constraints / 18 indexes), included a WAL-only (never flushed) commit, excluded a later commit, ran I4 → `[(A, C, B)]`, and enforced constraints. `clone db backup.rl` then `clone backup.rl restored` → I4 still correct. Existing destination → rc=1 `clone destination already exists`. | `clone` is **only in the standalone CLI binary**, not in `relate-db`; the image needs it too. Per CLI docs, a writer-held `.rl` *source* can't be cloned, so keep the live store as a directory. **The CLI sends telemetry by default** (see Risks) |
| 6 | I1, I2 (quantified path), I3, I4 on seeded data | **PASS** | `test_c6_queries.py`:<br>**I1:** 2 `relate.Path`s, as1→fc→ev_geo_c and as1→fs→ev_sig.<br>**I2:** `findings=[fs, g1, g2]` (depth 0–2 via `<-[:DERIVED_FROM]-+`), `assessments=[as1, as2]`, `assertions=[l1]`.<br>**I3:** (k1,k2), (k2,k1).<br>**I4 verbatim:** exactly one row `{entity_a:'A', via:'C', entity_b:'B', sigint_findings:[fs], geoint_findings:[fc, fb], windows:[{cell_c:'cell_2_2', cell_b:'cell_3_3', from:11:00Z, until:12:00Z}], disputed:false}`.<br>**Negatives, all `[]`:** SIGINT finding → `NEEDS_REVIEW` (via an I2-style retraction tx with Decision + AuditEvent); trusted direct A–B handle contact; manifest `RELATED_TO` A–B; B two cells away.<br>**Clearance:** DEMO-HIGH direct contact queried at rank 2 still returns the path and leaks no `fd` id. `DISPUTED` sets `disputed=true`. | Seed includes distractors (E far away in an overlapping window; E nearby in a non-overlapping window); both correctly excluded. The I2 sketch ends in `...`; the completed version is in `spikes/relate/queries.py` |
| 7 | Two investigations of the same scenario coexist | **PASS** | `test_c7_scoping.py`: inv1 and inv2 seeded with identical manifest ids, 10 entities each, `Entity {manifest_id:'A'}` ×2. I4 per investigation returns its own `{A,C,B}` with `sigint_findings=['inv1:fs']` / `['inv2:fs']`. I3 doesn't mix investigations. Retraction in inv1 leaves inv2's path intact. Re-seeding inv1 → `IntegrityError` and the whole tx rolls back (nodes 28 → 28). | |

### Extras (`uv run --project spikes/relate python spikes/relate/extras.py`)

| Measure | Observed |
|---|---|
| 10k nodes (100-char prop) + 10k rels | Load 167 ms. RSS 27 → 104 MiB (+77 MiB, including the Python-side row list). 3-hop var-length query avg 0.04 ms |
| Open empty + bootstrap DDL | 24.5 ms |
| `close()` (checkpoint) | 94 ms. On-disk size before close 4.2 MiB (WAL), after 0.3 MiB (Parquet shards) |
| Reopen + count (10k) | 32 ms |
| `read_only` open + query + close | 25 ms avg |
| SIGTERM, no handler | rc −15, no close ran. All 501 autocommitted writes present after reopen; lease released |
| SIGTERM, handler calls `conn.close()` | rc 0, clean close, 501 present |
| On-disk layout | `manifest.json`, `nodes/*.parquet`, `relationships/*.parquet`, `wal/`, `.relate/schema/schema.cypher`, lock files `.cypherlite.writer` / `.cypherlite.lock` |

## 4. Cypher feature compatibility (Relate 1.2.0, observed)

All rows come from `test_c6_queries.py::test_cypher_feature` and the I1–I4 tests.

| Feature (as used by I1–I4) | Result | Notes |
|---|---|---|
| `EXISTS { (a)-[:R]-(b) }` | ✅ | |
| `NOT EXISTS { MATCH … WHERE … }` (multi-pattern, correlated) | ✅ | Used 2× in I4 |
| `[:SUBJECT\|OBJECT]` type alternation | ✅ | |
| Pattern predicate in `WHERE` (I3) | ✅ | |
| Pattern expression in `RETURN` | ❌ `ProgrammingError: syntax error: UnexpectedSyntax: pattern expressions are not allowed in RETURN projection` | Same rule as Neo4j 5 / Cypher 25. Not used by I1–I4. Rewrite: `RETURN EXISTS { … }` ✅ |
| Cartesian `point.distance` (literal and stored props) | ✅ | √2 → 1.4142 |
| Point index use by planner | ⚠️ not used | Label scan + filter (see C1) |
| `datetime` comparison (stored, literal, Python param) | ✅ | Python tz-aware `datetime` ↔ Cypher DATETIME |
| `reduce(acc = [], x IN … \| CASE … END)` | ✅ | |
| Map literal in `collect(DISTINCT {…})` | ✅ | |
| `all(f IN [n1, n2] WHERE …)` | ✅ | |
| Quantified path `<-[:DERIVED_FROM]-+` | ✅ | |
| QPP group `(()<-[:R]-()){1,}` | ✅ | |
| Var-length `*1..` | ✅ | Unbounded stops at 32 hops (limitations.md) |
| `COUNT { }`, `COLLECT { }` subqueries | ✅ | |
| `max(CASE …)`, `'X' IN [..]`, `collect(DISTINCT) + collect(DISTINCT)` | ✅ | |
| `shortestPath` | ✅ | |
| `CALL { … } IN TRANSACTIONS` (autocommit) | ✅ | |

**No rewrites were needed for I1–I4.** ADR-007's fallback (split I4 into two queries joined in Python) is unnecessary.

## 5. Spec / ADR assumptions that were wrong or imprecise

| Assumption (where) | Reality (observed) | Impact |
|---|---|---|
| `relate.connect(path)`, `db.execute(query, params)` (§4.3) | Correct, but it's a DB-API connection. Transactions are `autocommit=False` + `commit()`/`rollback()`, with no context-manager tx | GraphRepository wraps this; trivial |
| "single user/agent per process" (§4.3, ADR-001) | Concretely: (1) a connection is thread-affine; (2) one **writable** handle per store, enforced by a file lease even inside one process; (3) `read_only` handles are allowed concurrently but are **frozen at open** | Confirms the single-lane design. "Reads may share the lane until S0 confirms safe concurrent reads" should become: reads go through the lane (RO handles are safe but stale) |
| "Backups use `relate clone` from a stopped or read-only store" (§4.3) | `clone` of a **directory** store works beside a live writer (it captures WAL-only commits). It is a separate CLI binary, not in `relate-db` | `make backup` can run hot. The Context Service image (or a backup sidecar) needs the CLI binary pinned to the same version |
| Point index on `Location.position` (§9.3, ADR-007 "point.distance with the point index") | The index is created, but the planner doesn't use it for `point.distance` / `withinBBox` | No impact on I4 (traversal-anchored) or at demo scale. Don't claim spatial-index performance |
| "Pin the version; upgrade requires re-running the spike suite" (ADR-001) | Five releases in ~2 weeks; `docs/limitations.md` on `main` already says 1.2.1 | The pin plus this suite are essential, not optional |
| Durability "fsync'd WAL" (§4.3) | Confirmed for process crash (`kill -9`). Default mode `full`, but `RELATE_DURABILITY_MODE=everysecond\|snapshot` silently weakens it | Context Service should refuse to start if that env var is set to anything but `full` |

## 6. Risks found

1. **Maturity / churn (high):** Alpha classifier, five releases in two weeks, upstream doesn't accept external PRs. Mitigation: exact pin, this suite in CI, and the `GraphRepository` seam (the Cypher is Neo4j-5-compatible as written, so the Neo4j fallback is still cheap).
2. **CLI telemetry on by default (medium, compliance):** the `relate` CLI spooled Mixpanel events for sending (`telemetry-spool-*`/`telemetry-claim-*` files; whether they were actually delivered was not verified) (`RELATE-CLI_STARTUP`, `RELATE-CLI_COMMAND {command:"clone"}`, with OS/arch/version and an install UUID; no paths or data) to `~/.config/relate/` on this machine during the spike, before `DO_NOT_TRACK=1` was added. Mitigation: set `DO_NOT_TRACK=1` (or `RELATE_TELEMETRY=0`) in every image and Makefile target that runs the CLI. A string scan of the Python native module found no telemetry endpoint. That is a weak check, so verify with a network-off run in the container.
3. **Stale reads if someone "optimises" with RO handles (medium):** they don't follow writes, so this must be documented in GraphRepository.
4. **WAL growth / reopen time (low):** with no checkpoint, reopen grew 9 → 146 ms over ~73k nodes of WAL. Mitigation: `flush()` on the lane periodically (e.g. when idle) and on graceful shutdown. Install a SIGTERM handler that closes the store; uvicorn's lifespan shutdown does this.
5. **Poisoned transactions (low):** any statement error needs an explicit `rollback()`; the lane must always do this.
6. **Not verified:** Linux/Docker behaviour, named-volume fsync semantics, power-loss durability, and behaviour beyond ~75k nodes.

## 7. Recommendation: GO-WITH-CONDITIONS (keep ADR-001)

Criteria 1–4, the gate for falling back to Neo4j Community, all passed (C1's DDL fully; the unused point index is not a gate item). I4 runs unmodified.

Conditions:
1. **All reads and writes go through the single lane thread** in S1. Do not use `read_only` handles for request serving.
2. **GraphRepository lane contract:** `autocommit=False`, one tx per domain op, `rollback()` on any exception, periodic `flush()`, `close()` on shutdown.
3. **Pin `relate-db==1.2.0` and CLI v1.2.0** (sha256-verified) together. Ship the CLI in the image for `make backup` (`relate clone <live-dir> <backup-dir|.rl>`, hot is fine for directory stores).
4. **Set `DO_NOT_TRACK=1`** (plus `RELATE_TELEMETRY=0`) everywhere the CLI runs. Fail startup if `RELATE_DURABILITY_MODE` is set to anything other than `full`.
5. **Re-run `spikes/relate` tests inside the Context Service Linux image** on the Compose named volume before S1 is closed. C4 (`kill -9`) and C5 are the important ones.
6. Remove "with the point index" from any performance claim. Treat the point index as harmless until the planner uses it.
