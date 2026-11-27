#!/usr/bin/env bash
# `make chaos` (S3b spec §14: AC12-a, AC13-a, AC13-b, AC13-c) against the real containers.
#
# Plain version: bring the stack up with the chaos override (task deadline 45 s, both agents in
# replay mode with a 20 s delay per call so there is a mid-task moment to break something), break
# one thing per scenario with `docker compose stop|kill|restart`, then read the orchestrator API
# and check the stack recovered, or ended in a clearly reported PARTIAL.
#
# Usage:  scripts/chaos.sh [ac12a|ac13a|ac13b|ac13c ...]    (default: all four, in that order)
#         NEXUS_UI_PORT=3000   port of the frontend proxy, the only published port
#
# Expected timings (CHAOS_DEADLINE_S = 45, the override's NEXUS_TASK_DEADLINE_SECONDS; the retry
# wait window is min(TIMED_OUT time + task deadline, investigation deadline 600 s), so it is
# derived from the task deadline, not a fixed 180 s):
#   ac12a  GEOINT TIMED_OUT <= 45 + 3 s after its deadline; PARTIAL(deadline_exceeded) about
#          45 s later (~90 s after create); the script waits at most 2 x 45 + 15 s.
#   ac13a  restart mid-task -> attempt 2 COMPLETED: 20 s replay delay + restart time, < 90 s.
#   ac13b  kill after dispatch -> leader lock goes stale (<= 15 s) -> resume sweep -> same end.
#   ac13c  cancel with Context Service down -> context_closed=false; after it is healthy again
#          the 30 s reconciler flips it to true within 30 s.
#
# Safety: every docker call is `docker compose -p nexus ...` on this repo's compose files, never a
# bare container name. Reads no secrets: .env is only passed to compose via --env-file, and the
# only value printed back from a container is NEXUS_REPLAY_LATENCY_MS. On exit (pass, fail or
# Ctrl-C) every non-terminal investigation is cancelled through the API first, then the stack is
# re-applied from compose.yaml alone with NEXUS_LLM_MODE="${NEXUS_LLM_MODE:-replay}": it never
# flips the agents to live unless the caller exported NEXUS_LLM_MODE=live. Set CHAOS_NO_RESTORE=1
# to leave the chaos configuration running.
set -uo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "No .env - run: make secrets" >&2; exit 1; }
FILES=(-f infrastructure/docker/compose.yaml)
CHAOS_FILES=("${FILES[@]}" -f infrastructure/docker/compose.chaos.yaml)
COMPOSE=(docker compose -p nexus "${CHAOS_FILES[@]}" --env-file .env)
BASE_COMPOSE=(docker compose -p nexus "${FILES[@]}" --env-file .env)

CHAOS_DEADLINE_S=45                              # must equal compose.chaos.yaml
WAIT_CAP_S=$((2 * CHAOS_DEADLINE_S + 15))        # two attempts' deadlines + tick/restart margin
CS_CLOSE_S=30                                    # AC13-c: reconciler interval
PORT="${NEXUS_UI_PORT:-3000}"
BASE_URL="http://127.0.0.1:${PORT}"
SCENARIO_ID=baseline-corroboration
ALL=(ac12a ac13a ac13b ac13c)
SERVICES=(context-service sigint-agent geoint-agent orchestrator)

# --- python helpers (HTTP + assertions); argv: BASE_URL, then scenario-specific args -----------
read -r -d '' PYLIB <<'PY'
import json, sys, time, urllib.error, urllib.request
from datetime import datetime

BASE = sys.argv[1]
TERMINAL = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}

def call(method, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")

def inv(i):
    try:
        s, b = call("GET", f"/api/v1/investigations/{i}")
    except (OSError, ValueError):
        return None  # proxy/orchestrator briefly away (kill, restart)
    return b if s == 200 else None

def task(d, agent):
    return next((t for t in d["tasks"] if t["agent_id"] == agent), None)

def events(i):
    out, cursor = [], None
    while True:
        s, b = call("GET", f"/api/v1/investigations/{i}/timeline?limit=100" + (f"&cursor={cursor}" if cursor else ""))
        out += b["items"]
        cursor = b.get("next_cursor")
        if not cursor:
            return out

def findings(i):
    s, b = call("GET", f"/api/v1/investigations/{i}/findings")
    if s != 200:
        fail(f"GET findings returned {s}")
    return b["findings"]

def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def wait(pred, timeout, step=0.5):
    end = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > end:
            return None
        time.sleep(step)

def fail(msg):
    print(f"  ASSERT: {msg}")
    sys.exit(1)

def check(cond, msg):
    if not cond:
        fail(msg)

def ending(d):
    return d["status"], d.get("status_reason")

def task_events(evs, agent):
    return [e["payload"] for e in evs
            if e["type"] == "task.status_changed" and e["payload"].get("agent_id") == agent]

def wait_terminal(i, cap):
    d = wait(lambda: (x := inv(i)) and x["status"] in TERMINAL and x, cap)
    if d is None:
        last = inv(i)
        fail(f"not terminal after {cap}s: " + (json.dumps({t['agent_id']: [t['status'], t['attempt']] for t in last['tasks']}) if last else "API unreachable"))
    return d

def no_duplicate_findings(i):
    fs = findings(i)
    ids = [f["id"] for f in fs]
    check(len(ids) == len(set(ids)), "duplicate finding ids")
    keys = [(f["source_node_id"], f["statement"]) for f in fs]
    check(len(keys) == len(set(keys)), "duplicate finding statements from one agent")
    for src in {f["source_node_id"] for f in fs}:
        runs = {(f["task_id"], f["attempt"]) for f in fs if f["source_node_id"] == src}
        check(len(runs) == 1, f"{src} findings come from several task runs: {sorted(runs)}")
    print(f"  findings: {len(fs)}, no duplicates, one task run per agent")
    return fs
PY

pyrun() {   # pyrun [args...] <<'PY' ... PY   (argv[1] = BASE_URL, argv[2..] = args)
  { printf '%s\n' "$PYLIB"; cat; } | python3 - "$BASE_URL" "$@"
}

# --- docker helpers ---------------------------------------------------------------------------
ensure_stack() {   # every service running + healthy, and the API through the proxy answering
  "${COMPOSE[@]}" up -d --no-deps --wait "${SERVICES[@]}" >/dev/null 2>&1 || return 1
  local n
  for n in $(seq 1 60); do
    curl -fsS --max-time 3 "${BASE_URL}/api/v1/system/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

wait_healthy() {   # wait_healthy <service> <seconds>
  local cid state n
  for n in $(seq 1 "$2"); do
    cid=$("${COMPOSE[@]}" ps -q "$1")
    state=$([ -n "$cid" ] && docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null)
    [ "$state" = healthy ] && return 0
    sleep 1
  done
  return 1
}

create_investigation() {   # prints the new investigation id
  python3 -c '
import json, sys, urllib.request
req = urllib.request.Request(sys.argv[1] + "/api/v1/investigations",
    data=json.dumps({"scenario_id": sys.argv[2]}).encode(), method="POST",
    headers={"content-type": "application/json"})
print(json.load(urllib.request.urlopen(req, timeout=10))["id"])' "$BASE_URL" "$SCENARIO_ID"
}

context_closed() {   # read-only SELECT inside the orchestrator container; prints 0, 1 or missing
  "${COMPOSE[@]}" exec -T orchestrator python -c '
import sqlite3, sys
c = sqlite3.connect("file:/data/orchestrator.sqlite?mode=ro", uri=True)
r = c.execute("select context_closed from investigations where id = ?", (sys.argv[1],)).fetchone()
print(r[0] if r else "missing")' "$1" 2>/dev/null
}

# --- scenarios (each returns 0 = PASS, 1 = FAIL; details go to stdout) -------------------------
scenario_ac12a() {
  echo "  stopping geoint-agent, then creating an investigation"
  "${COMPOSE[@]}" stop geoint-agent >/dev/null || return 1
  local id; id=$(create_investigation) || { echo "  ASSERT: create failed"; return 1; }
  echo "  investigation $id"
  pyrun "$id" "$CHAOS_DEADLINE_S" "$WAIT_CAP_S" <<'PY'
DL = float(sys.argv[3]); CAP = float(sys.argv[4]); ID = sys.argv[2]
d = wait_terminal(ID, CAP)
geo, sig = task(d, "geoint-agent"), task(d, "sigint-agent")
evs = events(ID)
check(geo['status'] == "TIMED_OUT" and geo['attempt'] == 1, f"geoint {geo['status']} attempt {geo['attempt']}, expected TIMED_OUT attempt 1")
check(sig['status'] == "COMPLETED", f"sigint {sig['status']}, expected COMPLETED")
timed_out = [ts(e["created_at"]) for e in evs if e["type"] == "task.status_changed"
             and e["payload"].get("agent_id") == "geoint-agent" and e["payload"]["to_state"] == "TIMED_OUT"]
check(len(timed_out) == 1, f"{len(timed_out)} TIMED_OUT events for geoint, expected 1")
late = (timed_out[0] - ts(geo["deadline"])).total_seconds()
print(f"  geoint TIMED_OUT {late:+.1f}s after its deadline (limit +3.0s)")
check(-1.0 <= late <= 3.0, f"TIMED_OUT {late:+.1f}s from the deadline, expected within +3s")
check(all(p.get("attempt") == 1 for p in task_events(evs, "geoint-agent")), "a geoint attempt 2 appeared while geoint was down")
check(ending(d) == ("PARTIAL", "deadline_exceeded"), f"ended {ending(d)}, expected PARTIAL deadline_exceeded")
done = next(ts(e["created_at"]) for e in reversed(evs) if e["type"] == "investigation.status_changed")
since = (done - timed_out[0]).total_seconds()
print(f"  PARTIAL(deadline_exceeded) {since:.1f}s after TIMED_OUT (window {DL:.0f}s, limit {DL + 6:.0f}s)")
check(since <= DL + 6, f"PARTIAL came {since:.1f}s after TIMED_OUT, expected <= {DL + 6:.0f}s")
fs = findings(ID)
srcs = {f["source_node_id"] for f in fs}
check("sigint-agent" in srcs and "geoint-agent" not in srcs, f"finding sources {sorted(srcs)}, expected only sigint-agent")
print(f"  sigint findings kept: {len(fs)}")
PY
}

scenario_ac13a() {
  local id; id=$(create_investigation) || { echo "  ASSERT: create failed"; return 1; }
  echo "  investigation $id"
  pyrun "$id" <<'PY' || return 1
d = wait(lambda: (x := inv(sys.argv[2])) and (task(x, "geoint-agent") or {}).get("status") in ("DISPATCHED", "RUNNING") and x, 30)
check(d, "geoint task never reached DISPATCHED/RUNNING within 30s")
print(f"  geoint {task(d, 'geoint-agent')['status']}, restarting it now")
PY
  "${COMPOSE[@]}" restart geoint-agent >/dev/null || { echo "  ASSERT: restart failed"; return 1; }
  pyrun "$id" "$WAIT_CAP_S" <<'PY'
ID = sys.argv[2]; CAP = float(sys.argv[3])
d = wait_terminal(ID, CAP)
geo, sig = task(d, "geoint-agent"), task(d, "sigint-agent")
evs = events(ID)
first = [p for p in task_events(evs, "geoint-agent") if p.get("attempt") == 1 and p["to_state"] in ("FAILED", "TIMED_OUT")]
print("  attempt 1 ended as: " + ", ".join(f"{p['to_state']}({p.get('reason')})" for p in first))
check(any(p["to_state"] == "FAILED" and p.get("reason") == "agent_restarted" for p in first), "attempt 1 did not end FAILED(agent_restarted)")
check(geo['attempt'] == 2 and geo['status'] == "COMPLETED", f"geoint attempt {geo['attempt']} {geo['status']}, expected attempt 2 COMPLETED")
check(sig['status'] == "COMPLETED", f"sigint {sig['status']}, expected COMPLETED")
check(ending(d) == ("PARTIAL", "agent_not_enabled"), f"ended {ending(d)}, expected PARTIAL agent_not_enabled (AC3-a)")
no_duplicate_findings(ID)
print(f"  geoint attempt 2 COMPLETED; investigation PARTIAL(agent_not_enabled)")
PY
}

scenario_ac13b() {
  local id; id=$(create_investigation) || { echo "  ASSERT: create failed"; return 1; }
  echo "  investigation $id"
  pyrun "$id" <<'PY' || return 1
d = wait(lambda: (x := inv(sys.argv[2])) and all((task(x, a) or {}).get("status") in ("DISPATCHED", "RUNNING") for a in ("sigint-agent", "geoint-agent")) and x, 30)
check(d, "both tasks never reached DISPATCHED/RUNNING within 30s")
print("  both tasks dispatched, killing the orchestrator now")
PY
  "${COMPOSE[@]}" kill orchestrator >/dev/null || { echo "  ASSERT: kill failed"; return 1; }
  "${COMPOSE[@]}" start orchestrator >/dev/null || { echo "  ASSERT: start failed"; return 1; }
  pyrun "$id" "$WAIT_CAP_S" <<'PY'
ID = sys.argv[2]; CAP = float(sys.argv[3])
d = wait_terminal(ID, CAP)
sig, geo = task(d, "sigint-agent"), task(d, "geoint-agent")
print(f"  sigint {sig['status']} attempt {sig['attempt']}; geoint {geo['status']} attempt {geo['attempt']}")
check(sig["status"] == "COMPLETED" and geo["status"] == "COMPLETED", "a specialist is not COMPLETED after the resume")
check(ending(d) == ("PARTIAL", "agent_not_enabled"), f"ended {ending(d)}, expected PARTIAL agent_not_enabled (same as AC3-a)")
no_duplicate_findings(ID)
PY
}

scenario_ac13c() {
  local id; id=$(create_investigation) || { echo "  ASSERT: create failed"; return 1; }
  echo "  investigation $id"
  pyrun "$id" <<'PY' || return 1
d = wait(lambda: (x := inv(sys.argv[2])) and x["status"] == "RUNNING" and x, 30)
check(d, "investigation never reached RUNNING within 30s")
PY
  echo "  stopping context-service, then cancelling"
  "${COMPOSE[@]}" stop context-service >/dev/null || { echo "  ASSERT: stop failed"; return 1; }
  pyrun "$id" <<'PY' || return 1
s, b = call("POST", f"/api/v1/investigations/{sys.argv[2]}/cancel", timeout=20)
check(s == 200, f"cancel returned {s}: {b}")
check(b['status'] == "CANCELLED" and b['context_closed'] is False, f"cancel response {b}, expected CANCELLED with context_closed false")
d = inv(sys.argv[2])
check(d and d["status"] == "CANCELLED", "investigation is not CANCELLED right after the cancel")
print("  cancel -> CANCELLED, context_closed=false")
PY
  [ "$(context_closed "$id")" = 0 ] || { echo "  ASSERT: context_closed is not 0 while Context Service is down"; return 1; }
  "${COMPOSE[@]}" start context-service >/dev/null || { echo "  ASSERT: start failed"; return 1; }
  wait_healthy context-service 60 || { echo "  ASSERT: context-service not healthy within 60s"; return 1; }
  local t0=$SECONDS closed="" n
  for n in $(seq 1 "$CS_CLOSE_S"); do
    closed=$(context_closed "$id")
    [ "$closed" = 1 ] && break
    sleep 1
  done
  echo "  context_closed=${closed:-?} after $((SECONDS - t0))s of Context Service being healthy (limit ${CS_CLOSE_S}s)"
  [ "$closed" = 1 ] || { echo "  ASSERT: context_closed did not become 1 within ${CS_CLOSE_S}s"; return 1; }
}

# --- driver ------------------------------------------------------------------------------------
cancel_open() {   # best effort: cancel every non-terminal investigation (404/409 are fine)
  pyrun <<'PY' || echo "WARNING: could not list/cancel open investigations" >&2
cursor, ids = None, []
for _ in range(50):
    s, b = call("GET", "/api/v1/investigations?limit=100" + (f"&cursor={cursor}" if cursor else ""), timeout=5)
    if s != 200:
        sys.exit(f"list returned {s}")
    ids += [i["id"] for i in b["items"] if i["status"] not in TERMINAL]
    cursor = b.get("next_cursor")
    if not cursor:
        break
for i in ids:
    try:
        s, _ = call("POST", f"/api/v1/investigations/{i}/cancel", timeout=10)
    except (OSError, ValueError) as e:
        s = type(e).__name__
    print(f"  cancel {i}: {s}")
print(f"  {len(ids)} open investigation(s) cancelled before restore")
PY
}

restore() {
  [ "${CHAOS_NO_RESTORE:-0}" = 1 ] && { echo "--- CHAOS_NO_RESTORE=1: chaos configuration left running"; return; }
  echo "--- cancelling open investigations before the restore"
  cancel_open
  local mode="${NEXUS_LLM_MODE:-replay}"
  echo "--- restoring the stack from compose.yaml alone (NEXUS_LLM_MODE=${mode}, 180 s deadline)"
  NEXUS_LLM_MODE="$mode" "${BASE_COMPOSE[@]}" up -d --wait >/dev/null 2>&1 \
    || echo "WARNING: could not restore the default configuration; run: make up" >&2
}

scenarios=("$@")
[ ${#scenarios[@]} -gt 0 ] || scenarios=("${ALL[@]}")
for s in "${scenarios[@]}"; do
  case " ${ALL[*]} " in *" $s "*) ;; *) echo "unknown scenario '$s' (known: ${ALL[*]})" >&2; exit 2 ;; esac
done

trap restore EXIT
echo "--- bringing the stack up with the chaos override (deadline ${CHAOS_DEADLINE_S}s, replay, 20 s delay)"
"${COMPOSE[@]}" up -d --build --wait || { echo "FAIL: stack did not come up" >&2; exit 1; }
ensure_stack || { echo "FAIL: stack not healthy" >&2; exit 1; }
delay=$("${COMPOSE[@]}" exec -T geoint-agent printenv NEXUS_REPLAY_LATENCY_MS 2>/dev/null | tr -d '\r')
[ "$delay" = 20000 ] || { echo "FAIL: geoint-agent NEXUS_REPLAY_LATENCY_MS='${delay}', the chaos override is not applied" >&2; exit 1; }

fails=0
for s in "${scenarios[@]}"; do
  echo "--- $s"
  start=$SECONDS
  if "scenario_$s"; then verdict=PASS; else verdict=FAIL; fails=$((fails + 1)); fi
  echo "$verdict $s ($((SECONDS - start))s)"
  ensure_stack || { echo "FAIL: stack did not recover after $s; stopping" >&2; fails=$((fails + 1)); break; }
done
[ "$fails" = 0 ] && echo "chaos: all ${#scenarios[@]} scenario(s) PASS" || echo "chaos: $fails FAIL" >&2
[ "$fails" = 0 ]
