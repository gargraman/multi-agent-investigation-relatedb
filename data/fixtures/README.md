# Evidence fixtures (spec §11.2, §11.3)

**SYNTHETIC.** Every handle, subject, place and event here is invented. Handles use the reserved
`.example.invalid` domains (RFC 2606). The grid is the fictional "Vellmark grid", in abstract
cartesian units. It is not a map.

```
data/fixtures/<node>/<scenario>/<evidence_id>.json   # one file = one evidence item
```

Each `<node>/` directory is mounted read-only into its own node only. The manifest
(`data/scenarios/<scenario>/manifest.yaml`) lists every file with its `sha256`. A node checks the
hash before it uses a file and fails the task with `evidence_integrity_error` on a mismatch.

Pydantic models: `nexus_contracts.scenario.fixtures` (`CommsLog`, `SightingLog`). Loader:
`nexus_contracts.scenario.load_scenario`.

## Common header

| Field | Type | Notes |
|---|---|---|
| `schema` | string | `nexus.fixture.sigint.comms/1` or `nexus.fixture.geoint.sightings/1` |
| `evidence_id` | string | Must equal the manifest evidence `id` |
| `synthetic` | `true` | Literal |
| `classification` | `DEMO-LOW \| DEMO-MID \| DEMO-HIGH` | Must equal the manifest entry |
| `summary` | string ≤ 280 | Short, shareable summary (the only text that may leave the node) |

All timestamps are UTC (`...Z`). Windows are half-open, `[from, until)` (ADR-007 G7).

## SIGINT: `nexus.fixture.sigint.comms/1`

Communication **metadata** only. There is no content.

| Field | Type | Notes |
|---|---|---|
| `collector` | string | Fictional collection point |
| `records[]` | list | |
| `records[].record_id` | string | `<evidence_id>-rNN` |
| `records[].channel` | `VOICE \| TEXT \| DATA` | |
| `records[].from_handle`, `to_handle` | string | Mentions, resolved by alias (§11.4). Different from each other |
| `records[].start` | UTC datetime | |
| `records[].duration_s` | int > 0 | Contact window = `[start, start + duration_s)` |

The detector finds recurring contact pairs and their windows, then emits a `LINKED_TO`
(`CONTACT`) between two `HANDLE` entities.

## GEOINT: `nexus.fixture.geoint.sightings/1`

| Field | Type | Notes |
|---|---|---|
| `sensor` | string | Fictional sensor |
| `observations[]` | list | |
| `observations[].observation_id` | string | `<evidence_id>-oNN` |
| `observations[].subject` | string | Mention of a PERSON/ASSET alias. Unknown mentions become UNRESOLVED entities |
| `observations[].x`, `y` | float | Raw point in grid units |
| `observations[].observed_from`, `observed_until` | UTC datetime | `until > from` |

The detector snaps each point to its cell: `col = floor((x - origin.x) / pitch)`, and likewise for
rows. That cell is the `LOCATION` entity whose `position` is the cell centre. The detector then
emits `PLACED_AT`. Co-location means centres within `1.5 × pitch` (same cell or one of the 8
neighbours) with overlapping windows.

## Signal vs noise

- **Signal** is hand-authored and committed. It holds the planted patterns that the oracle
  describes.
- **Noise** is written by `scripts/gen_scenario.py` (Faker, fixed seed). The evidence ids it owns
  are listed under `noise:` in `data/scenarios/<scenario>/generator.yaml`. File names and contents
  carry no signal/noise marker, so detectors and the LLM cannot tell the two apart.
- Noise rules: every noise contact has at least one handle that is not in the manifest. No noise
  sighting overlaps in time with another sighting of the same subject, or with any sighting of
  another subject within `1.5 × pitch`. So noise cannot add or remove hidden paths,
  contradictions or corroborations. `contracts/tests/test_scenario_oracle.py` re-derives this
  independently.
- Noise placement avoids the signal sightings. Editing a GEOINT signal file can therefore change
  the generated noise.

```bash
uv run python scripts/gen_scenario.py           # regenerate noise and refresh the manifest sha256 values
uv run python scripts/gen_scenario.py --check   # exit 1 on any drift (also run by the test suite)
```

The byte-identical output depends on the Faker version pinned in `uv.lock`. A Faker upgrade
shows up as `--check` drift. Regenerate the files and commit them.
