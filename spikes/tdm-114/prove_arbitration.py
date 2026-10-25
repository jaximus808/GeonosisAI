"""TDM-114 proof on a throwaway `make demo` stack (local SpacetimeDB, MuJoCo sim robot, seeded evals).

Reproduces the live TKT-0004 situation: two sim tickets on one sim robot, ticket A's collection rollout
paused for a takeover nobody claims, ticket B's baseline waiting. Shows the waiting text (the web floor
model run on the real rows), POST claim_now, then a third ticket and the automatic yield.

    cd services/orchestrator && uv run python ../../spikes/tdm-114/prove_arbitration.py \
        --root spikes/tdm-114/tmp/demo --stdb http://127.0.0.1:3114 --db tdm114demo --orch http://127.0.0.1:8114
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import time
import urllib.request
from pathlib import Path

from rdf_common.spacetime_client import SpacetimeClient

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "spikes" / "tdm-114" / "tmp"


def log(msg: str) -> None:
    print(f"[tdm114] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def http(url: str, body: dict | None = None, key: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **({"x-rdf-operator-key": key} if key else {})},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait(pred, timeout: float, what: str, every: float = 2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise SystemExit(f"timed out after {timeout:.0f}s waiting for {what}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--stdb", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--orch", required=True)
    ap.add_argument("--desc", default="Pick up the cube and place it in the empty plate.")
    a = ap.parse_args()
    tok = Path(glob.glob(f"{a.root}/**/tokens/orchestrator.token", recursive=True)[0])
    key = (tok.parent / "operator.key").read_text().strip()
    c = SpacetimeClient(a.stdb, a.db, tok.read_text().strip())
    sql = c.sql
    robot = sql("SELECT * FROM robot WHERE name = 'sim-yam-mujoco'")[0]
    rid = int(robot["id"])
    log(f"robot {rid} {robot['name']} {robot['status']}")

    def tickets():
        return {int(t["id"]): t for t in sql("SELECT * FROM ticket")}

    def new_ticket(recovery: bool = False) -> int:
        before = set(tickets())
        c.call_reducer("create_ticket", rid, a.desc, 0.8, 5000, "")
        tid = wait(lambda: next(iter(set(tickets()) - before), None), 30, "ticket row", 0.2)
        if recovery:  # the planner's user override: collect recovery takeovers (DAgger rollouts on the sim robot)
            c.call_reducer("append_event", "orchestrator", "planner.data_type_override",
                           json.dumps({"ticket_id": tid, "data_type": "recovery_takeover"}))
        log(f"created TKT-{tid:04d}{' (recovery_takeover override)' if recovery else ''}")
        return tid

    def queue() -> dict:
        return http(f"{a.orch}/robots/{rid}/queue")[1]

    def dump_floor(tid: int, tag: str) -> dict:
        rows = {t: sql(f"SELECT * FROM {t}") for t in ("ticket", "robot", "robot_command", "eval_run", "eval_trial", "event_log", "task")}
        p = OUT / f"rows-{tag}.json"
        p.write_text(json.dumps(rows, default=str))
        out = subprocess.run(["node", "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", str(REPO / "spikes/tdm-114/floor_from_rows.ts"),
                              str(p), str(tid)], capture_output=True, text=True, cwd=REPO / "apps/web")
        if out.returncode:
            raise SystemExit(out.stderr)
        f = json.loads(out.stdout)
        (OUT / f"floor-{tag}.json").write_text(json.dumps(f, indent=2))
        return f

    # ---- ticket A collects; wait until its rollout pauses for a takeover
    A = new_ticket(recovery=True)
    wait(lambda: tickets()[A]["status"] == "dispatch", 600, f"TKT-{A:04d} in dispatch (baseline -> Claude diagnose -> planner -> auto-approve)", 5)
    log(f"TKT-{A:04d} collecting")
    h = wait(lambda: (q := queue()).get("holder") and q["holder"]["state"] == "paused_for_takeover" and q["holder"], 600,
             "a collection rollout paused for a takeover", 3)
    log(f"holder: {json.dumps(h)}")

    # ---- ticket B enters baseline while A holds the robot
    B = new_ticket()
    wait(lambda: tickets()[B]["status"] == "baseline", 120, f"TKT-{B:04d} in baseline")
    time.sleep(6)
    runs_b = [r for r in sql("SELECT * FROM eval_run") if int(r["ticket_id"]) == B]
    log(f"TKT-{B:04d} baseline eval runs: {len(runs_b)} (expected 0: the robot is held)")
    q = queue()
    log(f"GET /robots/{rid}/queue: holder TKT-{q['holder']['ticket_id']:04d} {q['holder']['kind']} {q['holder']['state']}; "
        f"queue {[(x['ticket'], x['job'], x['state']) for x in q['queue']]}")
    f = dump_floor(B, "1-waiting")
    st = f["stations"][0]
    log(f"floor TKT-{B:04d} station 0: state={st['state']} activity={st['activity']!r}")
    log(f"floor waitingFor.actions={f['waitingFor']['actions']} robotQueue={[(x['ticketRef'], x['job'], x['state']) for x in f['robotQueue']]}")
    assert st["state"] == "waiting" and st["activity"].startswith("Waiting for sim-yam-mujoco. It's paused for a person to take over")

    # ---- claim_now
    code, body = http(f"{a.orch}/tickets/{B}/robot/claim_now", {"by": "tdm114-proof"}, key)
    log(f"POST claim_now -> {code} {json.dumps({k: body.get(k) for k in ('outcome', 'eval_outcome')})} preempted={json.dumps(body.get('preempted'))}")
    assert code == 200 and body["outcome"] == "preempted"
    run = wait(lambda: next((r for r in sql("SELECT * FROM eval_run") if int(r["ticket_id"]) == B and r["status"] == "done"), None),
               180, f"TKT-{B:04d} baseline done")
    log(f"TKT-{B:04d} baseline eval_run {run['id']} {run['status']} {run['successes']}/{run['trials']} (SEEDED)")
    f = dump_floor(A, "2-held")
    log(f"floor TKT-{A:04d} collect: {f['stations'][3]['state']} {f['stations'][3]['activity']!r}")

    # ---- auto-yield: ticket C waits behind a paused rollout for RDF_PAUSED_TAKEOVER_TIMEOUT_S
    wait(lambda: tickets()[B]["status"] not in ("baseline", "eval"), 120, f"TKT-{B:04d} left baseline (hold on A released)")
    h = wait(lambda: (q := queue()).get("holder") and q["holder"]["state"] == "paused_for_takeover" and q["holder"], 900,
             "another paused takeover", 3)
    log(f"holder again: TKT-{h['ticket_id']:04d} cmd {h['command_id']} paused for task #{h['task_id']}")
    C = new_ticket()
    wait(lambda: tickets()[C]["status"] == "baseline", 120, f"TKT-{C:04d} in baseline")
    t0 = time.time()
    ev = wait(lambda: next((e for e in sql("SELECT * FROM event_log WHERE type = 'robot.yielded'")), None), 300, "robot.yielded", 3)
    log(f"robot.yielded after ~{time.time() - t0:.0f}s: {ev['payload_json']}")
    run = wait(lambda: next((r for r in sql("SELECT * FROM eval_run") if int(r["ticket_id"]) == C and r["status"] == "done"), None),
               180, f"TKT-{C:04d} baseline done")
    log(f"TKT-{C:04d} baseline eval_run {run['id']} done {run['successes']}/{run['trials']}")
    log("PROOF OK")


if __name__ == "__main__":
    main()
