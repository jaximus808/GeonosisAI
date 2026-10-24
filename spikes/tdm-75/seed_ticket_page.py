"""TDM-75: seed a THROWAWAY local SpacetimeDB with one full ticket run, for checking /tickets/[id].

Never point this at maincloud. It calls reducers as the local publisher (owner) through the
`spacetime` CLI, plus three seed-only reducers (tdm_75_seed_*) that exist only in the local copy
of the module under spikes/tdm-75/tmp/ (contributor, dataset offer and QA rows).

    spacetime start --listen-addr 127.0.0.1:3075 --data-dir spikes/tdm-75/tmp/stdb-data
    spacetime server add tdm75local --url http://127.0.0.1:3075 --no-fingerprint
    spacetime publish -s tdm75local tdm75seed --yes --no-config -p spikes/tdm-75/tmp/spacetime/spacetimedb
    python3 spikes/tdm-75/seed_ticket_page.py --delay 1.5     # watch the page fill in live

Camera URIs point at two short test-pattern clips uploaded through the TDM-4 media API under
tickets/tdm75-test/ (override with --front/--wrist). A second ticket on a fake robot uses the
fake-robot harness's tags ("[FAKE]", cameras_json {"fake": true}) to check the fake labels.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time

SERVER = "tdm75local"
DB = "tdm75seed"
DELAY = 0.0
FRONT = "modal-vol://rdf-media/tickets/tdm75-test/episodes/clip/front.mp4"
WRIST = "modal-vol://rdf-media/tickets/tdm75-test/episodes/clip/wrist.mp4"
BASE_CKPT = "gs://openpi-assets/checkpoints/pi05_base/params"


def call(reducer: str, *args: object) -> None:
    cmd = ["spacetime", "call", "-s", SERVER, DB, reducer, *[json.dumps(a) for a in args]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{reducer} failed: {r.stderr.strip()[-400:]}")
    print(f"  {reducer}")
    if DELAY:
        time.sleep(DELAY)


def sql(q: str) -> list[list]:
    r = subprocess.run(["spacetime", "sql", "--server", SERVER, DB, "--format", "json", q],
                       capture_output=True, text=True, check=True)
    out = r.stdout[r.stdout.index("["):]
    return json.loads(out)[0]["rows"]


def last_id(table: str, where: str = "") -> int:
    rows = sql(f"SELECT id FROM {table}" + (f" WHERE {where}" if where else ""))
    return max(int(r[0]) for r in rows)


def some(v: object) -> dict:
    return {"some": v}


NONE = {"none": []}


def ts(micros: int) -> dict:
    return {"__timestamp_micros_since_unix_epoch__": micros}


def wilson(s: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    p = s / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def pct(x: float) -> int:
    return int(math.floor(x * 100 + 0.5))


def summary(s: int, n: int) -> str:
    lo, hi = wilson(s, n)
    return f"{s}/{n}, 95% CI {pct(lo)}–{pct(hi)}%"


def cams(front: str = "", wrist: str = "") -> str:
    return json.dumps({"front": front or FRONT, "wrist": wrist or WRIST})


def status(tid: int, to: str, note: str) -> None:
    call("set_ticket_status", tid, to, note)


def run_eval(tid: int, ckpt: str, outcomes: list[tuple[bool, str]], kind: str) -> int:
    call("start_eval_run", tid, ckpt, len(outcomes))
    run = last_id("eval_run", f"ticket_id = {tid}")
    for i, (ok, note) in enumerate(outcomes):
        dur = 14000 + i * 700
        call("create_episode", tid, 0, 0, kind, f"modal-vol://rdf-media/tickets/tdm75-test/episodes/{kind}-{i}",
             f"modal-vol://rdf-media/tickets/tdm75-test/episodes/{kind}-{i}/actions.parquet", cams(),
             dur * 30 // 1000, dur, NONE, NONE, some(ok), some(ckpt))
        ep = last_id("episode", f"ticket_id = {tid}")
        call("record_eval_trial", run, i, ok, dur, ep, note)
    s = sum(1 for ok, _ in outcomes if ok)
    lo, hi = wilson(s, len(outcomes))
    call("finalize_eval_run", run, s, len(outcomes), lo, hi, summary(s, len(outcomes)))
    return run


def main_ticket() -> int:
    print("robot + ticket")
    call("register_robot", "yam-1", "i2rt_yam", "v0.3.1", json.dumps({"front": "realsense-d405", "wrist": "usb-wrist"}))
    robot = last_id("robot", "name = 'yam-1'")
    call("create_ticket", robot, "Pick up the red cup and put it in the bin", 0.8, 5000, "")
    tid = last_id("ticket")
    status(tid, "baseline", "robot already onboarded")

    print("baseline eval")
    slip = "gripper closed on the cup, then it slipped out during the lift"
    miss = "approached ~2 cm left of the cup and closed on air"
    base = [(False, slip), (True, ""), (False, miss), (False, slip), (True, ""),
            (False, slip), (False, miss), (True, ""), (False, slip), (False, miss)]
    run_eval(tid, BASE_CKPT, base, "baseline")
    status(tid, "diagnose", "baseline 3/10")

    print("diagnose")
    eps = [int(r[0]) for r in sql(f"SELECT id FROM episode WHERE ticket_id = {tid}")]
    eps.sort()
    drop_eps = [eps[0], eps[3], eps[5]]
    miss_eps = [eps[2], eps[6], eps[9]]
    call("record_failure_cluster", tid, "drops mid-grasp", 4, json.dumps(drop_eps))
    c1 = last_id("failure_cluster", f"ticket_id = {tid}")
    call("record_failure_cluster", tid, "misses the cup", 3, json.dumps(miss_eps))
    c2 = last_id("failure_cluster", f"ticket_id = {tid}")
    call("append_event", "orchestrator.diagnose", "diagnose.clusters", json.dumps({
        "ticket_id": tid, "iteration": 0, "trials": 10, "successes": 3, "failures": 7,
        "clusters": [
            {"failure_cluster_id": c1, "label": "drops mid-grasp", "count": 4, "phase": "lift",
             "description": "The gripper closes on the cup but the cup slides out as the arm lifts.",
             "recoverable_mid_episode": True, "recoverability_reason": "The cup is still upright next to the gripper; a person can re-grasp it."},
            {"failure_cluster_id": c2, "label": "misses the cup", "count": 3, "phase": "approach",
             "description": "The arm stops about 2 cm left of the cup and closes on air.",
             "recoverable_mid_episode": True, "recoverability_reason": "The arm is paused near the cup; a small correction finishes the grasp."},
        ],
        "recoverable_failures": 7, "unrecoverable_failures": 0,
    }))
    status(tid, "plan_data", "2 failure clusters")

    print("data plan")
    reasoning = (
        "All 7 baseline failures happen after the policy has already reached the cup: 4 drop it during "
        "the lift and 3 close a little to the left of it. Both are recoverable mid-episode, so the "
        "cheapest fix is recovery takeovers that start from exactly those states, not full clean demos. "
        "6 takeovers at $5.00 is $30.00, inside the $50.00 budget, and leaves room for one more round."
    )
    call("record_data_plan", tid, "recovery_takeover", 6, reasoning,
         json.dumps({"failure_cluster_ids": [c1, c2], "recoverable_failures": 7, "failures": 7,
                     "alternative": {"data_type": "clean_teleop", "amount": 3, "est_cost_cents": 3000,
                                     "why_not": "clean demos would mostly re-teach the approach the policy already gets right"}}))
    plan = last_id("data_plan", f"ticket_id = {tid}")
    call("approve_spend", tid, plan, 3000, "jaxon (web)")
    status(tid, "dispatch", "plan approved")

    print("contributors + tasks")
    for name, dev in (("Maya R.", ["android_webxr"]), ("Devon K.", ["leader_arm"]), ("Priya S.", ["android_webxr"])):
        call("tdm_75_seed_contributor", name, dev)
    people = sorted(int(r[0]) for r in sql("SELECT id FROM contributor WHERE kind = 'contributor'"))
    tasks: list[int] = []
    for i in range(6):
        call("create_task", tid, "recovery_takeover", 500, json.dumps({"data_plan_id": plan,
             "instructions": "Take over when the arm pauses near the cup and finish the grasp."}))
        tasks.append(last_id("task", f"ticket_id = {tid}"))
    call("create_task", tid, "reset", 200, json.dumps({"instructions": "Put the cup back on the X."}))
    reset_task = last_id("task", f"ticket_id = {tid}")

    print("takeover episodes")
    now = int(time.time() * 1_000_000)
    takeover_eps: list[int] = []
    for i, task in enumerate(tasks[:5]):
        who = people[i % len(people)]
        call("claim_task", task, who)
        dur = 9000 + i * 400
        t0 = now + i * 60_000_000
        handback = some(ts(t0 + 6_000_000)) if i != 2 else NONE
        call("create_episode", tid, task, who, "recovery_takeover",
             f"modal-vol://rdf-media/tickets/tdm75-test/episodes/takeover-{i}",
             f"modal-vol://rdf-media/tickets/tdm75-test/episodes/takeover-{i}/actions.parquet",
             cams(), dur * 30 // 1000, dur, some(ts(t0)), handback, some(True), NONE)
        takeover_eps.append(last_id("episode", f"ticket_id = {tid}"))
        call("complete_task", task, json.dumps({"episode_id": takeover_eps[-1]}))
    call("claim_task", tasks[5], people[0])
    call("claim_task", reset_task, people[1])

    print("reviews + payments")
    status(tid, "review", "5 takeovers in")
    for i, ep in enumerate(takeover_eps):
        jerk = 41.2 if i == 2 else 12.0 + i
        ok = i != 2
        call("tdm_75_seed_qa", ep, "takeover_ts", True, json.dumps({"takeover_ts": True}))
        call("tdm_75_seed_qa", ep, "handback_ts", i != 2, json.dumps({"handback_ts": i != 2}))
        call("tdm_75_seed_qa", ep, "jerk", jerk < 25, json.dumps({"value": jerk, "threshold": 25.0}))
        reason = ("Smooth recovery; takeover and handback timestamps present; success confirmed on replay."
                  if ok else "Rejected: joint jerk 41.2 rad/s^3 is above the 25.0 limit and the handback "
                             "timestamp is missing, so the failure frames cannot be cut.")
        call("submit_review", ep, ok, reason, json.dumps({"jerk": jerk, "frame_drops": 0, "duration_ms": 9000 + i * 400}))
    pays = sorted(int(r[0]) for r in sql(f"SELECT id FROM payment WHERE ticket_id = {tid}"))
    for i, p in enumerate(pays[:3]):
        call("mark_payment_paid", p, "stripe_test", f"tr_test_tdm75_{i}")
    call("tdm_75_seed_offer", tid, "YAM Data Co.", 300, 10, 4, 3, "ingesting")

    print("training")
    status(tid, "train", "4 accepted episodes")
    accepted = [e for i, e in enumerate(takeover_eps) if i != 2]
    call("queue_training_run", tid, json.dumps(accepted), json.dumps({"lr": 2.5e-5, "steps": 2000, "batch_size": 32, "lora_rank": 16}))
    tr = last_id("training_run", f"ticket_id = {tid}")
    call("set_training_status", tr, "running", some("fc-01JTDM75LOCALSEED"), NONE,
         some(json.dumps({"loss_first": 0.412})), NONE)
    ckpt = f"modal-vol://rdf-ckpts/tickets/{tid}/iter0/step-2000"
    call("set_training_status", tr, "done", NONE, some(ckpt),
         some(json.dumps({"loss_first": 0.412, "loss_last": 0.087,
                          "loss_curve": [0.412, 0.30, 0.22, 0.17, 0.14, 0.12, 0.105, 0.095, 0.09, 0.087],
                          "sec_per_step": 0.41})), some(184))
    status(tid, "eval", "checkpoint ready")

    print("candidate eval")
    cand = [(True, ""), (True, ""), (False, slip), (True, ""), (True, ""),
            (True, ""), (True, ""), (False, miss), (True, ""), (True, "")]
    run_eval(tid, ckpt, cand, "eval")
    return tid


def fake_ticket() -> int:
    print("fake robot ticket")
    call("register_robot", "fake-yam-1", "fake_yam", "fake-tdm28",
         json.dumps({"fake": True, "tag": "[FAKE]", "cameras": {"wrist": "fake://wrist", "front": "fake://front"}}))
    robot = last_id("robot", "name = 'fake-yam-1'")
    call("create_ticket", robot, "Stack the blue block on the red block", 0.7, 2000, "")
    tid = last_id("ticket")
    status(tid, "baseline", "fake harness")
    call("start_eval_run", tid, BASE_CKPT, 3)
    run = last_id("eval_run", f"ticket_id = {tid}")
    for i, ok in enumerate([True, False, False]):
        d = f"file:///tmp/fake-robot/FAKE-ep-tdm75{i}"
        call("create_episode", tid, 0, 0, "baseline", d, f"{d}/actions.json",
             json.dumps({"fake": True, "tag": "[FAKE]", "cameras": {"wrist": f"{d}/wrist.mp4", "front": f"{d}/front.mp4"}}),
             150, 5000, NONE, NONE, some(ok), some(BASE_CKPT))
        ep = last_id("episode", f"ticket_id = {tid}")
        call("record_eval_trial", run, i, ok, 5000, ep, "[FAKE] simulated outcome")
    lo, hi = wilson(1, 3)
    call("finalize_eval_run", run, 1, 3, lo, hi, summary(1, 3))
    return tid


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=0.0, help="seconds between reducer calls (watch it live)")
    ap.add_argument("--only", choices=["main", "fake"], default=None)
    ap.add_argument("--front", default=FRONT)
    ap.add_argument("--wrist", default=WRIST)
    a = ap.parse_args()
    DELAY, FRONT, WRIST = a.delay, a.front, a.wrist
    if a.only != "fake":
        print("main ticket id", main_ticket())
    if a.only != "main":
        print("fake ticket id", fake_ticket())
