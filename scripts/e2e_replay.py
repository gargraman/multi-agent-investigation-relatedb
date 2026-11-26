"""Two-agent replay E2E against the running stack (S3a Task 11: AC3-a, AC4-d).

For each scenario: create an investigation through the frontend's same-origin proxy, poll it to a
terminal status, and check

- status ``PARTIAL`` with reason ``agent_not_enabled`` (analysis stays disabled in S3a);
- the SIGINT and GEOINT tasks both ``COMPLETED`` with ``llm_mode == "replay"``;
- each specialist task reports ``usage.replayed`` and has an ``llm.replayed`` timeline event;
- the committed findings, split by ``source_node_id``, pass the oracle subset check
  (``check_agent_subset``) for both agents.

Exit 0 when every scenario passes, 1 with the violations listed otherwise. The agents must already
run with ``NEXUS_LLM_MODE=replay`` (``make e2e-replay`` does that). The frontend API (S1) needs no
auth, so this script reads no secrets and no ``.env``.

Usage::

    uv run python scripts/e2e_replay.py --base-url http://127.0.0.1:3000 [--scenario S ...]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

from nexus_contracts.api.context import InvestigationContext
from nexus_contracts.api.investigations import Investigation
from nexus_contracts.common import AgentId
from nexus_contracts.scenario import load_manifest
from nexus_contracts.scenario.oracle_check import check_agent_subset

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ["baseline-corroboration", "contradiction", "prompt-injection"]
SPECIALISTS = (AgentId.SIGINT, AgentId.GEOINT)
TERMINAL = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}
TIMEOUT_S = 120.0
POLL_S = 1.0


def run_scenario(client: httpx.Client, scenario: str, timeout_s: float) -> list[str]:
    """All violations for one scenario (empty = pass)."""
    oracle = load_manifest(REPO_ROOT / "data" / "scenarios" / scenario).oracle
    resp = client.post("/api/v1/investigations", json={"scenario_id": scenario})
    if resp.status_code != 201:
        return [f"create returned {resp.status_code}: {resp.text[:300]}"]
    inv_id = resp.json()["id"]
    print(f"  {scenario}: created {inv_id}", flush=True)

    end = time.monotonic() + timeout_s
    while True:
        inv = Investigation.model_validate(client.get(f"/api/v1/investigations/{inv_id}").json())
        if inv.status in TERMINAL:
            break
        if time.monotonic() > end:
            states = {t.agent_id.value: t.status for t in inv.tasks}
            return [f"{inv_id} not terminal after {timeout_s:.0f}s: {inv.status} tasks={states}"]
        time.sleep(POLL_S)

    violations: list[str] = []
    reason = inv.status_reason.value if inv.status_reason else None
    if (inv.status, reason) != ("PARTIAL", "agent_not_enabled"):
        violations.append(f"status {inv.status}({reason}), expected PARTIAL(agent_not_enabled)")
    tasks = {t.agent_id: t for t in inv.tasks}
    for agent in SPECIALISTS:
        task = tasks.get(agent)
        if task is None:
            violations.append(f"no {agent.value} task")
            continue
        mode = task.llm_mode.value if task.llm_mode else None
        if task.status != "COMPLETED" or mode != "replay":
            violations.append(
                f"{agent.value} task {task.status} llm_mode={mode} error={task.error_code}, "
                "expected COMPLETED / replay"
            )
        elif task.usage is None or not task.usage.replayed:
            violations.append(f"{agent.value} task usage does not report replayed=true")

    # Zero model calls: every specialist task has an ``llm.replayed`` timeline event.
    resp = client.get(f"/api/v1/investigations/{inv_id}/timeline", params={"limit": 100})
    if resp.status_code != 200:
        return [*violations, f"timeline returned {resp.status_code}: {resp.text[:300]}"]
    replayed = {
        e["payload"].get("agent_id")
        for e in resp.json().get("items", [])
        if e.get("type") == "llm.replayed"
    }
    print(f"  {scenario}: llm.replayed events for {sorted(replayed)}", flush=True)
    violations.extend(
        f"no llm.replayed timeline event for {a.value}"
        for a in SPECIALISTS
        if a.value not in replayed
    )

    resp = client.get(f"/api/v1/investigations/{inv_id}/findings")
    if resp.status_code != 200:
        return [*violations, f"findings returned {resp.status_code}: {resp.text[:300]}"]
    findings = InvestigationContext.model_validate(resp.json()).findings
    for agent in SPECIALISTS:
        mine = [f for f in findings if f.source_node_id is agent]
        violations.extend(f"{agent.value}: {v}" for v in check_agent_subset(oracle, agent, mine))
        print(f"  {scenario}: {agent.value} {len(mine)} finding(s)", flush=True)
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--scenario", action="append", choices=SCENARIOS, dest="scenarios")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S, help="per scenario, seconds")
    args = parser.parse_args(argv)

    failed: dict[str, list[str]] = {}
    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        for scenario in args.scenarios or SCENARIOS:
            try:
                violations = run_scenario(client, scenario, args.timeout)
            except (httpx.HTTPError, ValueError) as exc:
                violations = [f"{type(exc).__name__}: {exc}"]
            print(f"{'OK  ' if not violations else 'FAIL'} {scenario}", flush=True)
            for v in violations:
                print(f"  - {v}", flush=True)
            if violations:
                failed[scenario] = violations

    total = len(args.scenarios or SCENARIOS)
    print(f"{total - len(failed)}/{total} scenarios OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
