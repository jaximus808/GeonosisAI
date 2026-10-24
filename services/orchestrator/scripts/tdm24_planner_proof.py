#!/usr/bin/env python3
"""TDM-24 proof: one REAL Claude planner call on a realistic FIXTURE, end to end through SpacetimeDB.

Everything here is a FIXTURE (synthetic eval trials and clusters, not robot data). It writes only to a
THROWAWAY local server; maincloud is refused, so no fixture plan can pollute the real evidence log.

    spacetime start -l 127.0.0.1:3924 --data-dir $TMPDIR/tdm24-stdb/data --non-interactive &
    (cd spacetime && spacetime publish -s http://127.0.0.1:3924 tdm24test --yes)
    cd services/orchestrator && uv run python scripts/tdm24_planner_proof.py --scenario drops
    #   --no-db   skip SpacetimeDB: build the fixture in memory, call Claude, print the decision

Flow (local DB): register_robot -> create_ticket -> baseline eval (10 trials, finalize with Wilson CI)
-> record_failure_cluster x N + a diagnose.clusters event (TDM-23's shape) -> ticket to plan_data ->
planner.gather_inputs (SQL rows) -> planner.plan (Claude) -> record_data_plan -> read back data_plan
and the planner.data_plan event_log row.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap

from rdf_common.spacetime_client import SpacetimeClient, load_dotenv
from rdf_orchestrator import planner as P
from rdf_orchestrator.stats import wilson_interval

FIXTURE_LABEL = "TDM-24 proof FIXTURE: synthetic eval trials and clusters, not robot data"

# A realistic cup-in-bin baseline: pi0.5 out of the box, 10 trials.
SCENARIOS = {
    "drops": {
        "task": "[FIXTURE] Pick up the paper cup from the table and put it in the blue bin",
        "successes": 3,
        "clusters": [
            {"label": "drops mid-grasp", "phase": "lift", "recoverable_mid_episode": True,
             "description": "Gripper closes on the cup rim, the cup slips out during the lift and lands upright.",
             "recoverability_reason": "The cup stays upright within reach, so a person can regrasp and finish.",
             "notes": ["cup slipped out of gripper while lifting", "lost grip ~5 cm above table",
                       "grasped rim, cup fell back during lift", "dropped cup on the way up"]},
            {"label": "misses the cup", "phase": "grasp", "recoverable_mid_episode": True,
             "description": "Arm reaches the cup but closes the gripper 2-3 cm to its left.",
             "recoverability_reason": "Arm is already next to the cup; a small correction completes the grasp.",
             "notes": ["closed gripper beside the cup", "fingers closed on air left of cup"]},
            {"label": "knocks cup over", "phase": "approach", "recoverable_mid_episode": False,
             "description": "Wrist hits the cup on the approach and tips it over.",
             "recoverability_reason": "A tipped cup needs a scene reset before anyone can finish.",
             "notes": ["wrist knocked cup over during approach"]},
        ],
    },
    "never": {
        "task": "[FIXTURE] Pick up the paper cup from the table and put it in the blue bin",
        "successes": 0,
        "clusters": [
            {"label": "never approaches the cup", "phase": "approach", "recoverable_mid_episode": False,
             "description": "Arm drifts toward the table edge and hovers; it never gets within 15 cm of the cup.",
             "recoverability_reason": "No near-success state: a takeover would be a full demonstration.",
             "notes": ["hovered over table edge, timed out"] * 4 + ["drifted left, never near cup"] * 3},
            {"label": "freezes at start", "phase": "start", "recoverable_mid_episode": False,
             "description": "Policy outputs near-zero actions; the arm stays at home until timeout.",
             "recoverability_reason": "Nothing to recover from; the policy lacks the basic behaviour.",
             "notes": ["no motion for 60 s", "stayed at home pose", "barely moved, timed out"]},
        ],
    },
}


def build_rows_in_memory(sc: dict) -> dict[str, list[dict]]:
    """--no-db: the same fixture as table rows, shaped like the bus mirror."""
    trials, fails = [], [n for c in sc["clusters"] for n in c["notes"]]
    for i in range(10):
        ok = i < sc["successes"]
        trials.append({"id": 100 + i, "eval_run_id": 3, "trial_index": i, "success": ok,
                       "duration_ms": 31000 + 900 * i if ok else 42000 + 1500 * i, "episode_id": 0,
                       "failure_note": None if ok else fails[i - sc["successes"]]})
    lo, hi = wilson_interval(sc["successes"], 10)
    clusters, diag = [], []
    for cid, c in enumerate(sc["clusters"], start=11):
        clusters.append({"id": cid, "ticket_id": 1, "iteration": 0, "label": c["label"], "description": "",
                         "count": len(c["notes"]), "example_episode_ids": []})
        diag.append({"failure_cluster_id": cid, **{k: c[k] for k in ("label", "phase", "recoverable_mid_episode",
                                                                    "description", "recoverability_reason")},
                     "count": len(c["notes"])})
    payload = {"ticket_id": 1, "iteration": 0, "eval_run_id": 3, "fixture": FIXTURE_LABEL, "clusters": diag,
               "reasoning": "FIXTURE diagnosis: grouped by the failure notes."}
    return {
        "ticket": [{"id": 1, "task_description": sc["task"], "target_success": 0.8, "budget_cents": 5000,
                    "approved_cents": 0, "spent_cents": 0, "status": "plan_data", "iteration": 0}],
        "run_settings": [{"ticket_id": 1, "eval_trials": 10, "success_threshold": 0.8, "require_ci_low": False,
                          "budget_cents": 5000, "target_episodes": 5, "max_iterations": 3, "hyperparams_json": "{}"}],
        "dataset": [{"id": 1, "ticket_id": 1, "accepted_count": 0, "total_count": 0}],
        "eval_run": [{"id": 3, "ticket_id": 1, "kind": "baseline", "status": "done", "trials": 10,
                      "successes": sc["successes"], "ci_low": lo, "ci_high": hi, "checkpoint_uri": "pi05_base",
                      "finished_at": 1}],
        "eval_trial": trials, "failure_cluster": clusters, "data_plan": [], "dataset_offer": [], "contributor": [],
        "event_log": [{"id": 1, "type": "diagnose.clusters", "payload_json": json.dumps(payload)}],
    }


def seed_local_db(db: SpacetimeClient, sc: dict) -> int:
    """Write the fixture through the real reducers; returns the ticket id."""
    db.call_reducer("register_robot", "tdm24-fixture-yam", "yam", "fixture", "{}")
    robot = max(r["id"] for r in db.sql("SELECT * FROM robot WHERE name = 'tdm24-fixture-yam'"))
    db.call_reducer("create_ticket", robot, sc["task"], 0.8, 5000, "")
    tid = max(r["id"] for r in db.sql("SELECT * FROM ticket"))
    db.call_reducer("set_ticket_status", tid, "baseline", "TDM-24 fixture")
    db.call_reducer("start_eval_run", tid, "pi05_base", 10)
    run = max(r["id"] for r in db.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}"))
    notes = [n for c in sc["clusters"] for n in c["notes"]]
    for i in range(10):
        ok = i < sc["successes"]
        db.call_reducer("record_eval_trial", run, i, ok, 31000 + 900 * i if ok else 42000 + 1500 * i, 0,
                        "" if ok else notes[i - sc["successes"]])
    lo, hi = wilson_interval(sc["successes"], 10)
    db.call_reducer("finalize_eval_run", run, sc["successes"], 10, lo, hi, "TDM-24 fixture baseline")
    db.call_reducer("set_ticket_status", tid, "diagnose", "TDM-24 fixture")
    for c in sc["clusters"]:
        db.call_reducer("record_failure_cluster", tid, c["label"], len(c["notes"]), "[]")
    ids = {r["label"]: r["id"] for r in db.sql(f"SELECT * FROM failure_cluster WHERE ticket_id = {tid}")}
    payload = {"ticket_id": tid, "iteration": 0, "eval_run_id": run, "fixture": FIXTURE_LABEL,
               "reasoning": "FIXTURE diagnosis: grouped by the failure notes.",
               "clusters": [{"failure_cluster_id": ids[c["label"]], "count": len(c["notes"]),
                             **{k: c[k] for k in ("label", "phase", "recoverable_mid_episode", "description",
                                                  "recoverability_reason")}} for c in sc["clusters"]]}
    db.call_reducer("append_event", "test.tdm24", "diagnose.clusters", json.dumps(payload))
    db.call_reducer("set_ticket_status", tid, "plan_data", "TDM-24 fixture")
    return tid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://127.0.0.1:3924")
    ap.add_argument("--db", default="tdm24test")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="drops")
    ap.add_argument("--no-db", action="store_true")
    a = ap.parse_args()
    sc = SCENARIOS[a.scenario]
    print(f"== {FIXTURE_LABEL}\n== scenario: {a.scenario}")

    db = None
    if a.no_db:
        tables = build_rows_in_memory(sc)
        rows = lambda name: tables.get(name, [])  # noqa: E731
        ticket = tables["ticket"][0]
    else:
        if "maincloud" in a.host:
            sys.exit("refusing to write fixture rows to maincloud; use a local server")
        load_dotenv()
        db = SpacetimeClient(host=a.host, db=a.db)  # token: SPACETIME_TOKEN (the local publisher), never printed
        tid = seed_local_db(db, sc)
        rows = lambda name: db.sql(f"SELECT * FROM {name}")  # noqa: E731
        ticket = db.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]
        print(f"== seeded local ticket {tid} (status {ticket['status']})")

    inputs = P.gather_inputs(rows, ticket)
    inputs.fixture = FIXTURE_LABEL
    g = P.guardrail(inputs)
    print(f"== guardrail: {g.rule} -> {g.required_type}  ({g.why})")
    print(f"== calling {P.MODEL} (effort {P.EFFORT}) ...")
    out = P.plan(inputs, P.anthropic_client(), log=lambda m, f: print(f"   {m}: {f}"))
    if out.blocked:
        sys.exit(f"blocked: {out.blocked}")
    ev = out.evidence
    print(f"\nDECISION (fixture): {out.data_type} x {out.amount} @ {out.unit_price_cents}c "
          f"(clusters {out.failure_cluster_ids}); dataset offer: {ev['dataset_offer']}")
    print(f"served by {ev['model']['served']}; attempts: "
          + json.dumps([{k: x[k] for k in ('message_id', 'data_type', 'amount', 'violations', 'usage')}
                        for x in ev['model']['attempts']]))
    print("REASONING:\n" + textwrap.fill(out.reasoning, 100))

    if db is not None:
        db.call_reducer("record_data_plan", *out.reducer_args())
        plan = db.sql(f"SELECT * FROM data_plan WHERE ticket_id = {out.ticket_id}")[-1]
        evt = [e for e in db.sql("SELECT * FROM event_log") if e["type"] == "planner.data_plan"][-1]
        p = json.loads(evt["payload_json"])
        assert plan["reasoning"] == out.reasoning == p["reasoning"], "reasoning not stored verbatim"
        assert plan["data_type"] == out.data_type and plan["amount"] == out.amount
        assert plan["est_cost_cents"] <= inputs.budget_cents
        print(f"\n== recorded data_plan {plan['id']}: {plan['data_type']} x {plan['amount']}, "
              f"est {plan['est_cost_cents']}c, status {plan['status']}, clusters {plan['failure_cluster_ids']}")
        print(f"== event_log {evt['id']} planner.data_plan, evidence.fixture = {p['evidence'].get('fixture')!r}")
        print("OK: reasoning stored verbatim in data_plan and event_log")


if __name__ == "__main__":
    main()
