"""TDM-23 local E2E seeder: robot + ticket + 9 recorded episodes + a 10-trial baseline eval built from
the labelled fixture (services/orchestrator/tests/fixtures/tdm23_eval_run.json), then finalize it.
Synthetic test data on a THROWAWAY local server only (rows tagged test.tdm23). Prints the eval run id.

    uv run --project services/orchestrator python spikes/tdm-23-diagnose/seed_eval.py --host ... --db ... --token ...
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from rdf_common.spacetime_client import SpacetimeClient
from rdf_orchestrator.stats import wilson_interval

FIX = Path(__file__).resolve().parents[2] / "services/orchestrator/tests/fixtures/tdm23_eval_run.json"


def one(stdb, q):
    rows = stdb.sql(q)
    assert rows, q
    return rows[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--no-finalize", action="store_true")
    a = ap.parse_args()
    d = json.loads(FIX.read_text())
    s = SpacetimeClient(host=a.host, db=a.db, token=a.token, timeout=20)
    s.call_reducer("register_robot", "test.tdm23-yam", "yam", "test", "{}")
    robot = one(s, "SELECT * FROM robot WHERE name = 'test.tdm23-yam'")
    s.call_reducer("create_ticket", robot["id"], d["ticket"]["task_description"], 0.8, 5000, "")
    ticket = max(s.sql(f"SELECT * FROM ticket WHERE robot_id = {robot['id']}"), key=lambda r: r["id"])
    tid = ticket["id"]
    s.call_reducer("set_ticket_status", tid, "baseline", "test.tdm23 seed")
    ep_ids = {}
    for t in d["trials"]:
        if not t["episode_id"]:
            continue
        before = {r["id"] for r in s.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}")}
        cams = json.dumps({"exo": f"modal-vol://rdf-media/tickets/{tid}/episodes/test-{t['trial_index']}/exo.mp4"})
        s.call_reducer("create_episode", tid, 0, 0, "baseline",
                       f"modal-vol://rdf-media/tickets/{tid}/episodes/test-{t['trial_index']}/",
                       "", cams, 0, t["duration_ms"], None, None, t["success"], "test.tdm23")
        new = [r["id"] for r in s.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}") if r["id"] not in before]
        ep_ids[t["trial_index"]] = new[0]
    s.call_reducer("start_eval_run", tid, d["eval_run"]["checkpoint_uri"], 10)
    run = max(s.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}"), key=lambda r: r["id"])
    for t in d["trials"]:
        s.call_reducer("record_eval_trial", run["id"], t["trial_index"], t["success"], t["duration_ms"],
                       ep_ids.get(t["trial_index"], 0), t["failure_note"] or "")
    if not a.no_finalize:
        k = sum(t["success"] for t in d["trials"])
        lo, hi = wilson_interval(k, 10)
        s.call_reducer("finalize_eval_run", run["id"], k, 10, lo, hi, "test.tdm23 baseline")
    print(json.dumps({"ticket_id": tid, "eval_run_id": run["id"], "episode_ids_by_trial": ep_ids}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
