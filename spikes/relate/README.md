# Relate S0 spike (ADR-001)

A standalone uv project. It is **not** a member of the root workspace (the root `pyproject.toml` excludes `spikes/*`). It pins `relate-db==1.2.0`.
Findings: `specs/01-architecture/spike-report-relate.md`.

## Re-run everything (from the repo root)

```bash
# 1. The relate CLI (needed only for C5 `relate clone`; it is not in the Python wheel)
mkdir -p spikes/relate/store/bin && cd spikes/relate/store/bin
curl -fLO https://github.com/relatedb/relate/releases/download/v1.2.0/relate-darwin-arm64   # or relate-linux-amd64 / -arm64
curl -fLO https://github.com/relatedb/relate/releases/download/v1.2.0/SHA256SUMS
grep ' relate-darwin-arm64$' SHA256SUMS | shasum -a 256 -c && chmod +x relate-darwin-arm64
cd -
# The CLI sends anonymous usage telemetry by default. Export DO_NOT_TRACK=1 (the tests already set it).

# 2. Criteria C1–C7 (about 15 s). Use -s to see the evidence lines.
uv run --project spikes/relate pytest spikes/relate/tests -s -q
C4_ROUNDS=20 uv run --project spikes/relate pytest spikes/relate/tests/test_c4_durability.py -s -q   # more kill -9 rounds

# 3. Extras: memory, open/close time, SIGTERM
uv run --project spikes/relate python spikes/relate/extras.py
```

If the CLI is somewhere else, set `RELATE_CLI=/path/to/relate`. Without the CLI, C5 is skipped.

## Layout

| File | Purpose |
|---|---|
| `common.py` | Schema DDL (spec §9.3), seed data that plants the ADR-007 I4 pattern, store helpers. Stores live under `store/tmp/` (git-ignored) |
| `queries.py` | I1–I4, verbatim from spec §10 / ADR-007 §3 (I2 is completed after the sketch's `...`) |
| `writelane.py` | Prototype of the §4.3 write lane: one thread owns the connection, and each domain op is one transaction |
| `app.py` | Minimal FastAPI app on the write lane (C3) |
| `writer_proc.py` | Subprocess writer for the C4 `kill -9` test |
| `tests/test_c1_schema.py` … `test_c7_scoping.py` | One file per ADR-001 exit criterion |
| `extras.py` | Memory footprint, open/close/reopen timings, SIGTERM behaviour |
