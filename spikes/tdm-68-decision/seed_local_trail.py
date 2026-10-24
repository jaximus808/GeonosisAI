#!/usr/bin/env python3
"""TDM-68 local proof: build one whole decision chain through the REAL reducers on a THROWAWAY
local SpacetimeDB, then read it back with decision.build (the code behind GET /tickets/{id}/decision).

Everything seeded here is a FIXTURE: fake-camera episodes (cameras_json {"fake": true}, FAKE- uris,
"[FAKE]" failure notes, so the dashboard paints them magenta), clusters and a diagnose.clusters
event written by actor "test.tdm68". The planner step is the real TDM-24 planner (one real Claude
call) on those fixture rows, with ``inputs.fixture`` set so the plan is labelled FIXTURE. maincloud
is refused.

    spacetime start --listen-addr 127.0.0.1:3968 --data-dir spikes/tdm-68-decision/tmp/stdb/data --non-interactive &
    (cd spacetime && spacetime publish -s http://127.0.0.1:3968 tdm68test --yes)
    cd services/orchestrator && uv run python ../../spikes/tdm-68-decision/seed_local_trail.py
    #  --no-claude   skip the planner call (stops after the clusters: the trail shows the plan as missing)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from rdf_common.spacetime_client import AsyncSpacetimeClient, SpacetimeClient, load_dotenv
from rdf_orchestrator import decision
from rdf_orchestrator import planner as P
from rdf_orchestrator.stats import wilson_interval

FIXTURE = "TDM-68 local proof FIXTURE: fake-camera episodes and seeded clusters, not robot data"
NOTES = {1: "cup slipped out of the gripper during lift", 2: "lost grip ~5 cm above the table",
         4: "grasped the rim, cup fell back during lift", 6: "dropped the cup on the way to the bin",
         7: "cup slid out while transporting", 8: "arm never moved toward the cup", 9: "froze at the home pose"}
CLUSTERS = [
    {"label": "drops mid-grasp", "phase": "lift", "recoverable_mid_episode": True, "trials": [1, 2, 4, 6, 7],
     "description": "The gripper closes on the cup but it slips out during lift or transport and lands upright.",
     "recoverability_reason": "The cup stays upright within reach, so a person can regrasp and finish."},
    {"label": "never approaches the cup", "phase": "start", "recoverable_mid_episode": False, "trials": [8, 9],
     "description": "The arm stays at or near home and never moves toward the cup.",
     "recoverability_reason": "No progress to recover from; a takeover would be a full demo."},
]


def seed(db: SpacetimeClient) -> int:
    db.call_reducer("register_robot", "test.tdm68-yam", "fake_yam", "fake", json.dumps({"fake": True}))
    robot = max(r["id"] for r in db.sql("SELECT * FROM robot WHERE name = 'test.tdm68-yam'"))
    task = f"[FIXTURE] test.tdm68 put the cup in the bin ({int(time.time())})"
    db.call_reducer("create_ticket", robot, task, 0.8, 5000, "")
    tid = max(r["id"] for r in db.sql("SELECT * FROM ticket") if r["task_description"] == task)
    db.call_reducer("set_ticket_status", tid, "baseline", "test.tdm68 fixture")
    db.call_reducer("start_eval_run", tid, "pi05_base", 10)
    run = max(r["id"] for r in db.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}"))
    ep_of = {}
    for i in range(10):
        ok = i not in NOTES
        uri = f"fake://tdm68/tickets/{tid}/FAKE-ep-{i}"
        db.call_reducer("create_episode", tid, 0, 0, "baseline", uri, "",
                        json.dumps({"fake": True, "cameras": {"wrist": f"{uri}/wrist.mp4"}}), 900, 30000 + 700 * i,
                        None, None, ok, "pi05_base")
        ep = max(r["id"] for r in db.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}"))
        ep_of[i] = ep
        db.call_reducer("record_eval_trial", run, i, ok, 30000 + 700 * i, ep, "" if ok else f"[FAKE] {NOTES[i]}")
    lo, hi = wilson_interval(3, 10)
    db.call_reducer("finalize_eval_run", run, 3, 10, lo, hi, "test.tdm68 fixture baseline 3/10")
    db.call_reducer("set_ticket_status", tid, "diagnose", "test.tdm68 fixture")
    for c in CLUSTERS:
        db.call_reducer("record_failure_cluster", tid, c["label"], len(c["trials"]),
                        json.dumps([ep_of[i] for i in c["trials"][:3]]))
    ids = {r["label"]: r["id"] for r in db.sql(f"SELECT * FROM failure_cluster WHERE ticket_id = {tid}")}
    payload = {"ticket_id": tid, "iteration": 0, "eval_run_id": run, "eval_kind": "baseline",
               "checkpoint_uri": "pi05_base", "trials": 10, "successes": 3, "failures": 7, "model": "fixture",
               "fixture": FIXTURE, "fake": True,
               "clusters": [{"failure_cluster_id": ids[c["label"]], "count": len(c["trials"]),
                             "trial_indices": c["trials"], "episode_ids": [ep_of[i] for i in c["trials"]],
                             "example_episode_ids": [ep_of[i] for i in c["trials"][:3]],
                             **{k: c[k] for k in ("label", "description", "phase", "recoverable_mid_episode",
                                                  "recoverability_reason")}} for c in CLUSTERS],
               "recoverable_failures": 5, "unrecoverable_failures": 2,
               "reasoning": "[FIXTURE] Seeded grouping for the TDM-68 local proof, not a model's output."}
    db.call_reducer("append_event", "test.tdm68", "diagnose.clusters", json.dumps(payload))
    db.call_reducer("set_ticket_status", tid, "plan_data", "test.tdm68 fixture")
    return tid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://127.0.0.1:3968")
    ap.add_argument("--db", default="tdm68test")
    ap.add_argument("--no-claude", action="store_true")
    ap.add_argument("--out", default=str(Path(__file__).parent / "tmp" / "trail.json"))
    a = ap.parse_args()
    if "maincloud" in a.host:
        sys.exit("refusing to write fixture rows to maincloud; use a local server")
    load_dotenv()
    db = SpacetimeClient(host=a.host, db=a.db)  # token: SPACETIME_TOKEN, never printed
    tid = seed(db)
    print(f"== seeded local ticket {tid}")

    if not a.no_claude:
        ticket = db.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]
        inputs = P.gather_inputs(lambda n: db.sql(f"SELECT * FROM {n}"), ticket)
        inputs.fixture = FIXTURE
        print(f"== calling {P.MODEL} for the plan ...")
        out = P.plan(inputs, P.anthropic_client())
        if out.blocked:
            sys.exit(f"planner blocked: {out.blocked}")
        db.call_reducer("record_data_plan", *out.reducer_args())
        plan = db.sql(f"SELECT * FROM data_plan WHERE ticket_id = {tid}")[-1]
        print(f"== data_plan {plan['id']}: {plan['data_type']} x {plan['amount']}")
        db.call_reducer("approve_spend", tid, plan["id"], 0, "human:web")
        db.call_reducer("set_ticket_status", tid, "dispatch", "test.tdm68 fixture")
        for k in range(2):
            db.call_reducer("create_task", tid, plan["data_type"], 0,
                            json.dumps({"instructions": "[FIXTURE] test.tdm68", "data_plan_id": plan["id"]}))

    async def read() -> dict:
        async with AsyncSpacetimeClient(host=a.host, db=a.db) as c:
            return await decision.build(c.sql, tid)

    trail = asyncio.run(read())
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(trail, indent=1, default=str))
    d = trail["decisions"][0]
    bad = [lk for lk in d["links"] if lk["ok"] is False]
    print(f"== trail written to {a.out}")
    print(f"   source eval {d['source_eval']['eval_run_id']} ({d['source_eval']['linked_by']}), "
          f"clusters {[c['failure_cluster_id'] for c in d['clusters']]}, "
          f"plan {(d['plan'] or {}).get('data_plan_id')}, tasks {[t['task_id'] for t in d['tasks']]}")
    print(f"   links: {len(d['links']) - len(bad)}/{len(d['links'])} ok; broken: {[b['check'] for b in bad]}")
    print(f"   missing: {d['missing']}")
    print(f"   before_any_human_action: {d['autonomy']['before_any_human_action']}; "
          f"first human action: {d['autonomy']['first_human_action']}")


if __name__ == "__main__":
    main()
