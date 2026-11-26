"""Oracle-gated replay recording (S2 spec FR-051, §10.4; ADR-006 E5, E8).

Usage:
    uv run python scripts/record_replay.py --scenario prompt-injection --agent sigint-agent
    uv run python scripts/record_replay.py --scenario contradiction --agent geoint-agent
    uv run python scripts/record_replay.py --scenario baseline-corroboration --agent sigint-agent \
        [--replay-root data/replay] [--data-root data] [--dry-run]

Runs the agent pipeline **in-process and live** (real OpenAI call) wrapped in a
``RecordingProvider``, checks the produced findings against the scenario oracle's per-agent subset
(``nexus_contracts.scenario.oracle_check.check_agent_subset``), and writes recordings only when the
gate passes. On failure nothing is written, the violations are printed and the exit code is 1.

Exit codes: 0 pass (recordings written, or would be with ``--dry-run``); 1 gate failed;
2 refused / misconfigured (``NEXUS_LLM_MODE=replay``, no key, unknown scenario or agent).

Environment (never printed):
    OPENAI_API_KEY          required
    OPENAI_MODEL_EXTRACT    dated snapshot, default ``gpt-5.4-mini-2026-03-17``
    NEXUS_EXTRA_CA_FILE / EXTRA_CA_FILE / SSL_CERT_FILE
                            extra CA appended to certifi (corporate TLS proxy, S2 spec §10.3)
    NEXUS_MAX_TOKENS_PER_INVESTIGATION
                            investigation cap; the task budget is ``floor(token_share x cap)``
    NEXUS_LLM_MODE          must not be ``replay`` (recording a replay is meaningless)

Recordings land under ``<replay-root>/<agent>/<scenario>/<key>.json`` (S2 spec G10, §8.5); the
``RecordingProvider`` derives the sub-directories from each call's key material.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from nexus_contracts.a2a.task import TaskRequest, TraceContext
from nexus_contracts.common import AgentId, Classification
from nexus_contracts.scenario import Oracle, ScenarioManifest, load_manifest
from nexus_contracts.scenario.oracle_check import check_agent_subset, find_forbidden

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gpt-5.4-mini-2026-03-17"
DEFAULT_MAX_TOKENS = 60000
CONTRACT_VERSION = "1.0"
OPENAI_TIMEOUT_S = 45.0  # S2 spec NFR-001
OPENAI_MAX_RETRIES = 2
SUPPORTED_AGENTS = {AgentId.SIGINT, AgentId.GEOINT}  # S3a added GEOINT (spec §10.4)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class Refused(Exception):
    """Configuration or environment the tool refuses to run with (exit 2)."""


class Recorder(Protocol):
    def write(self, replay_root: Path) -> Any: ...


class OutcomeLike(Protocol):
    """The fields of WP-B's ``nexus_sigint.pipeline.PipelineOutcome`` the gate reads."""

    status: Any
    batch: Any  # FindingBatchRequest | None
    summary: Any
    error: Any


@dataclass
class GateResult:
    passed: bool
    violations: list[str] = field(default_factory=list)
    written: Any = None


# -- pure helpers (unit-tested without WP-B) ----------------------------------------------------


def _ulid() -> str:
    value = (int(datetime.now(UTC).timestamp() * 1000) << 80) | int.from_bytes(os.urandom(10))
    return "".join(_CROCKFORD[(value >> (5 * i)) & 0x1F] for i in reversed(range(26)))


def build_task_request(
    manifest: ScenarioManifest,
    agent: AgentId,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    now: datetime | None = None,
) -> TaskRequest:
    """A ``TaskRequest`` from the manifest template task for ``agent``. Ids and times are
    volatile; the replay key strips them (spec §13.2), so fresh values are fine."""
    tasks = [t for t in manifest.template.tasks if t.agent is agent]
    if not tasks:
        raise Refused(f"scenario {manifest.scenario_id} has no template task for {agent}")
    (task,) = tasks
    now = now or datetime.now(UTC)
    return TaskRequest(
        task_id=f"tsk_{_ulid()}",
        investigation_id=f"inv_{_ulid()}",
        task_type=task.task_type,
        objective=manifest.objective,
        input_refs=list(task.input_refs),
        scenario_id=manifest.scenario_id,
        classification_ceiling=Classification.DEMO_HIGH,  # agent clearance (FR-023)
        attempt=1,
        token_budget=max(1, math.floor(task.token_share * max_tokens)),
        deadline=now + timedelta(seconds=180),
        contract_version=CONTRACT_VERSION,
        trace_context=TraceContext(
            traceparent=f"00-{os.urandom(16).hex()}-{os.urandom(8).hex()}-01"
        ),
    )


def _value(x: Any) -> str:
    return str(getattr(x, "value", x))


def evaluate(outcome: OutcomeLike, oracle: Oracle, agent: AgentId) -> list[str]:
    """All gate violations for one pipeline outcome (empty = pass)."""
    violations: list[str] = []
    status = _value(outcome.status)
    if status != "COMPLETED":
        err = outcome.error
        code = _value(getattr(err, "code", err)) if err is not None else "none"
        violations.append(f"pipeline status {status} (error {code}), expected COMPLETED")
    findings: Sequence[Any] = list(outcome.batch.findings) if outcome.batch is not None else []
    if status == "COMPLETED":
        violations.extend(check_agent_subset(oracle, agent, findings))
    violations.extend(find_forbidden(oracle, [("task summary", outcome.summary)]))
    return violations


def decide_and_write(
    outcome: OutcomeLike,
    oracle: Oracle,
    agent: AgentId,
    recorder: Recorder,
    replay_root: Path,
    dry_run: bool,
) -> GateResult:
    """The recording gate (spec §10.4 step 4): write only on pass, never on fail or dry run."""
    violations = evaluate(outcome, oracle, agent)
    if violations:
        return GateResult(passed=False, violations=violations)
    if dry_run:
        return GateResult(passed=True)
    return GateResult(passed=True, written=recorder.write(replay_root))


def check_environment(env: dict[str, str]) -> None:
    if env.get("NEXUS_LLM_MODE", "").strip().lower() == "replay":
        raise Refused("NEXUS_LLM_MODE=replay: recording needs a live model; unset it or use live")
    if not env.get("OPENAI_API_KEY"):
        raise Refused("OPENAI_API_KEY is not set")


def extra_ca_file(env: dict[str, str]) -> Path | None:
    for name in ("NEXUS_EXTRA_CA_FILE", "EXTRA_CA_FILE", "SSL_CERT_FILE"):
        value = env.get(name)
        if value and Path(value).is_file() and Path(value).stat().st_size > 0:
            return Path(value)
    return None


def report(result: GateResult, scenario: str, agent: AgentId, dry_run: bool) -> None:
    if result.passed:
        verb = "would write" if dry_run else "wrote"
        print(f"PASS {agent} / {scenario}: oracle subset matched; {verb} recordings")
        if result.written is not None:
            print(f"  {result.written}")
        return
    print(f"FAIL {agent} / {scenario}: oracle gate failed; nothing written", file=sys.stderr)
    for v in result.violations:
        print(f"  - {v}", file=sys.stderr)


# -- live wiring (agent pipeline interfaces, imported lazily) -------------------------------


def _pipeline_run(agent: AgentId) -> Any:
    """The agent's ``pipeline.run`` coroutine function (imported lazily)."""
    if agent is AgentId.GEOINT:
        from nexus_geoint.pipeline import run
    else:
        from nexus_sigint.pipeline import run
    return run


async def run_live(
    request: TaskRequest,
    manifest: ScenarioManifest,
    fixtures_root: Path,
    env: dict[str, str],
    *,
    agent: AgentId,
) -> tuple[Any, Recorder]:
    from pydantic import SecretStr

    from nexus_shared.llm import OpenAIProvider, RecordingProvider, TokenBudget, build_ssl_context

    run = _pipeline_run(agent)

    inner = OpenAIProvider(
        model=env.get("OPENAI_MODEL_EXTRACT") or DEFAULT_MODEL,
        api_key=SecretStr(env["OPENAI_API_KEY"]),
        timeout_s=OPENAI_TIMEOUT_S,
        max_retries=OPENAI_MAX_RETRIES,
        ssl_context=build_ssl_context(extra_ca_file(env)),
    )
    recorder = RecordingProvider(inner)
    outcome = await run(
        request,
        provider=recorder,
        manifest=manifest,
        fixtures_root=fixtures_root,
        budget=TokenBudget(request.token_budget),
    )
    return outcome, recorder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--agent", required=True, help="agent id, e.g. sigint-agent")
    parser.add_argument("--replay-root", type=Path, default=REPO_ROOT / "data" / "replay")
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--dry-run", action="store_true", help="run and gate, never write")
    args = parser.parse_args(argv)
    env = dict(os.environ)

    try:
        try:
            agent = AgentId(args.agent)
        except ValueError as exc:
            raise Refused(f"unknown agent {args.agent!r}") from exc
        if agent not in SUPPORTED_AGENTS:
            raise Refused(
                f"{agent} has no live pipeline yet (supported: sigint-agent, geoint-agent)"
            )
        check_environment(env)
        scenario_dir = args.data_root / "scenarios" / args.scenario
        if not (scenario_dir / "manifest.yaml").is_file():
            raise Refused(f"unknown scenario {args.scenario!r} under {args.data_root}")
        manifest = load_manifest(scenario_dir)
        max_tokens = int(env.get("NEXUS_MAX_TOKENS_PER_INVESTIGATION") or DEFAULT_MAX_TOKENS)
        request = build_task_request(manifest, agent, max_tokens=max_tokens)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    outcome, recorder = asyncio.run(
        run_live(request, manifest, args.data_root / "fixtures", env, agent=agent)
    )
    result = decide_and_write(
        outcome, manifest.oracle, agent, recorder, args.replay_root, args.dry_run
    )
    report(result, args.scenario, agent, args.dry_run)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
