"""Deterministic noise generator for the Nexus scenario pack (spec §11.2).

Usage:
    uv run python scripts/gen_scenario.py                      # (re)write noise + manifest sha256s
    uv run python scripts/gen_scenario.py --check              # exit 1 if committed files drift
    uv run python scripts/gen_scenario.py --scenario contradiction
    uv run python scripts/gen_scenario.py --data-root /tmp/copy/data --check

For each ``data/scenarios/<id>/`` with a ``generator.yaml``:
  1. Generate the evidence files listed under ``noise`` with Faker seeded from ``seed``
     (fixed seed => byte-identical output; the Faker version is pinned by uv.lock).
  2. Recompute the sha256 of EVERY evidence file (signal and noise) and rewrite only the
     quoted ``sha256: "…"`` values in ``manifest.yaml`` (comments and layout are preserved).
  3. Write ``data/agent-manifests/<id>.json`` = the manifest minus its ``oracle`` block (what the
     agents mount; the oracle never reaches an agent container).
Hand-authored signal files (evidence ids not listed under ``noise``) are read, never written.

Noise is constrained so it cannot change what the oracle expects:
  - every SIGINT noise contact has at least one UNRESOLVED handle (not in the manifest), so noise
    never links two manifest people and cannot create or break an I4 path;
  - a GEOINT noise sighting is rejected if its window overlaps another sighting of the same
    subject (no new contradictions), or overlaps a sighting of any other subject within
    1.5 x pitch (no new co-locations / corroborations / hidden paths).
The oracle test (contracts/tests/test_scenario_oracle.py) re-derives all of this independently.

``--check`` never writes. It renders everything in memory and compares bytes with the committed
noise files and manifests; any drift (edited noise, edited signal without a sha256 refresh,
missing file) prints the offending paths and exits 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from faker import Faker

from nexus_contracts.common import AgentId, EntityType
from nexus_contracts.scenario import (
    GEOINT_SCHEMA,
    SIGINT_SCHEMA,
    AgentManifest,
    ScenarioManifest,
    load_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
NOISE_DOMAINS = ["relay.example.invalid", "mesh.example.invalid"]
AGENT_MANIFESTS_DIRNAME = "agent-manifests"
COLLECTORS = ["relay-tap-north", "relay-tap-south", "mesh-tap-east", "mesh-tap-west"]
SENSORS = ["mast-03", "mast-07", "mast-north-11", "drone-relay-2", "drone-relay-4"]
SUBJECT_KINDS = ["vehicle", "pedestrian", "cyclist"]
DWELL_MINUTES = [20, 30, 45, 60, 90, 120]
MAX_TRIES = 500


@dataclass(frozen=True)
class Sighting:
    subject: str  # resolved entity id, or "U:<mention>" for an unresolved subject
    cx: float  # cell centre
    cy: float
    start: datetime
    end: datetime


def dump_json(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def ts(value: datetime) -> str:
    return value.strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=UTC)


class ScenarioGenerator:
    def __init__(self, scenario_dir: Path, fixtures_root: Path) -> None:
        self.scenario_dir = scenario_dir
        self.fixtures_root = fixtures_root
        self.manifest: ScenarioManifest = load_manifest(scenario_dir)
        self.config = yaml.safe_load((scenario_dir / "generator.yaml").read_text("utf-8"))
        self.fake = Faker("en_US")
        self.fake.seed_instance(int(self.config["seed"]))
        self.rng = self.fake.random
        self.aliases = self.manifest.alias_index()
        tr = self.config["time_range"]
        self.t0, self.t1 = parse_ts(tr["start"]), parse_ts(tr["end"])
        self.noise_ids = {
            AgentId.SIGINT: list(self.config["noise"]["sigint"]["ids"]),
            AgentId.GEOINT: list(self.config["noise"]["geoint"]["ids"]),
        }
        self._check_noise_ids()

    def _check_noise_ids(self) -> None:
        refs = {e.id: e for e in self.manifest.evidence}
        for owner, ids in self.noise_ids.items():
            for eid in ids:
                if eid not in refs:
                    raise SystemExit(f"{self.manifest.scenario_id}: noise id {eid} not in manifest")
                if refs[eid].owner_node is not owner:
                    raise SystemExit(f"{self.manifest.scenario_id}: {eid} is not {owner} evidence")

    # -- helpers -------------------------------------------------------------------------------

    def _fresh_token(self, make: Any, taken: set[str]) -> str:
        for _ in range(MAX_TRIES):
            token = make()
            if token not in taken and token.casefold() not in {a.casefold() for a in self.aliases}:
                taken.add(token)
                return token
        raise SystemExit("could not generate a unique noise token")

    def _random_time(self, slot_minutes: int, tail: timedelta = timedelta(0)) -> datetime:
        slots = int((self.t1 - self.t0 - tail).total_seconds() // (slot_minutes * 60))
        return self.t0 + timedelta(minutes=slot_minutes * self.rng.randrange(slots))

    def _entities(self, *types: EntityType) -> list[str]:
        return [e.id for e in self.manifest.entities if e.type in types]

    def _mention(self, entity_id: str) -> str:
        ent = self.manifest.entity(entity_id)
        return ent.aliases[0] if ent.aliases else ent.id

    # -- SIGINT --------------------------------------------------------------------------------

    def gen_sigint(self) -> dict[str, dict[str, Any]]:
        cfg = self.config["noise"]["sigint"]
        taken: set[str] = set()
        unresolved = [
            self._fresh_token(
                lambda: (
                    f"{self.fake.word()}-{self.rng.randint(10, 99)}@"
                    f"{self.rng.choice(NOISE_DOMAINS)}"
                ),
                taken,
            )
            for _ in range(int(cfg["unresolved_handles"]))
        ]
        resolved = [self._mention(h) for h in self._entities(EntityType.HANDLE)]
        lo, hi = cfg["records_per_file"]
        out: dict[str, dict[str, Any]] = {}
        for eid in self.noise_ids[AgentId.SIGINT]:
            records = []
            for _ in range(self.rng.randint(lo, hi)):
                a = self.rng.choice(resolved + unresolved)
                b = self.rng.choice([u for u in unresolved if u != a])  # >= 1 unresolved party
                if self.rng.random() < 0.5:
                    a, b = b, a
                channel = self.rng.choice(["VOICE", "TEXT", "DATA"])
                duration = {"VOICE": self.rng.randint(20, 900), "TEXT": 1}.get(
                    channel, self.rng.randint(5, 300)
                )
                records.append(
                    {
                        "channel": channel,
                        "from_handle": a,
                        "to_handle": b,
                        "start": self._random_time(1, timedelta(hours=1)),
                        "duration_s": duration,
                    }
                )
            records.sort(key=lambda r: (r["start"], r["from_handle"], r["to_handle"]))
            for i, r in enumerate(records, start=1):
                r["record_id"] = f"{eid}-r{i:02d}"
                r["start"] = ts(r["start"])
            out[eid] = {
                "schema": SIGINT_SCHEMA,
                "evidence_id": eid,
                "synthetic": True,
                "classification": self.manifest.evidence_ref(eid).classification.value,
                "collector": self.rng.choice(COLLECTORS),
                "summary": f"Synthetic comms metadata batch ({len(records)} records).",
                "records": records,
            }
        return out

    # -- GEOINT --------------------------------------------------------------------------------

    def _signal_sightings(self) -> list[Sighting]:
        grid = self.manifest.grid
        noise = set(self.noise_ids[AgentId.GEOINT])
        sightings = []
        for ref in self.manifest.evidence:
            if ref.owner_node is not AgentId.GEOINT or ref.id in noise:
                continue
            data = json.loads((self.fixtures_root / ref.file).read_bytes())
            for o in data["observations"]:
                col, row = grid.cell_of(o["x"], o["y"]) or (-1, -1)
                c = grid.cell_centre(col, row)
                subject = self.aliases.get(o["subject"], f"U:{o['subject']}")
                start, end = parse_ts(o["observed_from"]), parse_ts(o["observed_until"])
                sightings.append(Sighting(subject, c.x, c.y, start, end))
        return sightings

    def _conflicts(self, s: Sighting, existing: list[Sighting]) -> bool:
        radius = self.manifest.grid.neighbour_radius
        for e in existing:
            if not (s.start < e.end and e.start < s.end):
                continue
            if e.subject == s.subject:
                return True  # would risk a new contradiction
            if math.dist((s.cx, s.cy), (e.cx, e.cy)) <= radius + 1e-9:
                return True  # would create a new co-location
        return False

    def gen_geoint(self) -> dict[str, dict[str, Any]]:
        cfg = self.config["noise"]["geoint"]
        grid = self.manifest.grid
        taken: set[str] = set()
        unresolved = [
            self._fresh_token(
                lambda: (
                    f"unidentified-{self.rng.choice(SUBJECT_KINDS)}-"
                    f"{self.fake.lexify('??', letters='ABCDEFGHJKLMNPRSTUVWXYZ')}"
                ),
                taken,
            )
            for _ in range(int(cfg["unresolved_subjects"]))
        ]
        subjects = self._entities(EntityType.PERSON, EntityType.ASSET) + [
            f"U:{u}" for u in unresolved
        ]
        cells = [
            grid.centre_cell(e.position) for e in self.manifest.entities if e.position is not None
        ]
        existing = self._signal_sightings()
        lo, hi = cfg["observations_per_file"]
        out: dict[str, dict[str, Any]] = {}
        for eid in self.noise_ids[AgentId.GEOINT]:
            observations = []
            for _ in range(self.rng.randint(lo, hi)):
                for _try in range(MAX_TRIES):
                    subject = self.rng.choice(subjects)
                    col, row = self.rng.choice(cells)  # type: ignore[misc]
                    dwell = timedelta(minutes=self.rng.choice(DWELL_MINUTES))
                    start = self._random_time(10, dwell)
                    x = round(grid.origin.x + (col + self.rng.uniform(0.08, 0.92)) * grid.pitch, 1)
                    y = round(grid.origin.y + (row + self.rng.uniform(0.08, 0.92)) * grid.pitch, 1)
                    c = grid.cell_centre(col, row)
                    s = Sighting(subject, c.x, c.y, start, start + dwell)
                    if not self._conflicts(s, existing):
                        existing.append(s)
                        break
                else:
                    raise SystemExit(f"{eid}: could not place a non-conflicting noise sighting")
                mention = subject[2:] if subject.startswith("U:") else self._mention(subject)
                observations.append(
                    {
                        "subject": mention,
                        "x": x,
                        "y": y,
                        "observed_from": s.start,
                        "observed_until": s.end,
                    }
                )
            observations.sort(key=lambda o: (o["observed_from"], o["subject"]))
            for i, o in enumerate(observations, start=1):
                o["observation_id"] = f"{eid}-o{i:02d}"
                o["observed_from"] = ts(o["observed_from"])
                o["observed_until"] = ts(o["observed_until"])
            out[eid] = {
                "schema": GEOINT_SCHEMA,
                "evidence_id": eid,
                "synthetic": True,
                "classification": self.manifest.evidence_ref(eid).classification.value,
                "sensor": self.rng.choice(SENSORS),
                "summary": f"Synthetic sightings batch ({len(observations)} observations).",
                "observations": observations,
            }
        return out

    # -- rendering -------------------------------------------------------------------------------

    def render(self) -> dict[Path, bytes]:
        """Return {path: bytes} for every generated noise file and the updated manifest."""
        rendered: dict[Path, bytes] = {}
        generated = {**self.gen_sigint(), **self.gen_geoint()}
        hashes: dict[str, str] = {}
        for ref in self.manifest.evidence:
            path = self.fixtures_root / ref.file
            if ref.id in generated:
                data = dump_json(generated[ref.id])
                rendered[path] = data
            elif path.is_file():
                data = path.read_bytes()
            else:
                raise SystemExit(f"missing hand-authored signal file: {ref.file}")
            hashes[ref.id] = hashlib.sha256(data).hexdigest()

        manifest_path = self.scenario_dir / "manifest.yaml"
        text = manifest_path.read_text("utf-8")
        for eid, digest in hashes.items():
            pattern = re.compile(
                r"(\{id: " + re.escape(eid) + r",[^}\n]*?sha256: \")[0-9a-f]{64}(?=\")"
            )
            text, n = pattern.subn(lambda m, d=digest: m.group(1) + d, text)
            if n != 1:
                raise SystemExit(f"{manifest_path}: expected one sha256 for {eid}, found {n}")
        rendered[manifest_path] = text.encode("utf-8")
        rendered[self.agent_manifest_path()] = self.render_agent_manifest(text)
        return rendered

    def agent_manifest_path(self) -> Path:
        return (
            self.scenario_dir.parent.parent
            / AGENT_MANIFESTS_DIRNAME
            / (f"{self.manifest.scenario_id}.json")
        )

    @staticmethod
    def render_agent_manifest(manifest_text: str) -> bytes:
        """The manifest minus its ``oracle`` block: what agents mount (never the oracle)."""
        raw = yaml.safe_load(manifest_text)
        raw.pop("oracle")
        data = dump_json(raw)
        AgentManifest.model_validate_json(data)  # the projection must stay loadable by agents
        return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify, never write; exit 1 on drift")
    parser.add_argument("--scenario", action="append", help="limit to scenario id (repeatable)")
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "data")
    args = parser.parse_args(argv)

    scenarios_root = args.data_root / "scenarios"
    fixtures_root = args.data_root / "fixtures"
    dirs = sorted(p.parent for p in scenarios_root.glob("*/generator.yaml"))
    if args.scenario:
        dirs = [d for d in dirs if d.name in set(args.scenario)]
    if not dirs:
        print("no scenarios with generator.yaml found", file=sys.stderr)
        return 1

    drift: list[str] = []
    for scenario_dir in dirs:
        rendered = ScenarioGenerator(scenario_dir, fixtures_root).render()
        for path, data in sorted(rendered.items()):
            rel = path.relative_to(args.data_root)
            if args.check:
                if not path.is_file() or path.read_bytes() != data:
                    drift.append(str(rel))
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.is_file() or path.read_bytes() != data:
                    path.write_bytes(data)
                    print(f"wrote {rel}")
    if drift:
        print("scenario drift (run `uv run python scripts/gen_scenario.py`):", file=sys.stderr)
        for rel in drift:
            print(f"  {rel}", file=sys.stderr)
        return 1
    if args.check:
        print(f"scenario pack up to date ({len(dirs)} scenarios)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
