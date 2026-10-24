"""TDM-68 evidence trail: decision.assemble on in-memory rows (FIXTURE rows, test only) + the router."""
from __future__ import annotations

import copy
import json
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from rdf_orchestrator import decision
from rdf_orchestrator.decision_api import decision_router, get_decision_stdb

T0 = 1_792_900_000_000_000  # micros
S = 1_000_000
REASONING = ('The baseline failed 7 of 10 trials (3/10, 95% CI 11-60%). "drops mid-grasp" (5 of 7 failures) '
             'happens after the grasp, so recovery takeovers from those states are the targeted fix.')


def ev(eid, ts, actor, typ, payload):
    return {"id": eid, "ts": T0 + ts * S, "actor": actor, "type": typ, "payload_json": json.dumps(payload)}


def fixture() -> dict:
    tid, run = 7, 3
    trials, episodes = [], []
    fails = {1: "dropped cup during lift", 2: "cup slipped", 4: "lost grip", 5: "dropped", 7: "slipped out",
             8: "never reached the cup", 9: "froze at start"}
    for i in range(10):
        eid = 40 + i
        ok = i not in fails
        episodes.append({"id": eid, "ticket_id": tid, "task_id": 0, "contributor_id": 0, "kind": "eval",
                         "review_status": "not_reviewed", "storage_uri": f"s3://rdf/tickets/7/episodes/{eid}",
                         "cameras_json": json.dumps({"wrist": f"s3://rdf/tickets/7/episodes/{eid}/wrist.mp4"}),
                         "actions_uri": "", "frame_count": 900, "duration_ms": 30000, "success": ok,
                         "checkpoint_uri": "pi05_base", "created_at": T0 + i * S})
        trials.append({"id": 100 + i, "eval_run_id": run, "trial_index": i, "success": ok, "duration_ms": 30000,
                       "episode_id": eid, "failure_note": fails.get(i), "created_at": T0 + i * S})
    bought = [{"id": 60 + k, "ticket_id": tid, "task_id": 200 + k, "contributor_id": 9, "kind": "recovery_takeover",
               "review_status": "accepted", "storage_uri": f"s3://rdf/tickets/7/episodes/{60 + k}",
               "cameras_json": "{}", "actions_uri": "", "frame_count": 600, "duration_ms": 20000, "success": True,
               "checkpoint_uri": None, "created_at": T0 + (300 + k) * S} for k in range(2)]
    diag_payload = {
        "ticket_id": tid, "iteration": 0, "eval_run_id": run, "eval_kind": "baseline", "trials": 10, "successes": 3,
        "failures": 7, "model": "claude-opus-5-5", "reasoning": "Five failures lose the cup after grasping it.",
        "clusters": [
            {"failure_cluster_id": 12, "label": "drops mid-grasp", "description": "Cup slips out during lift.",
             "phase": "lift", "recoverable_mid_episode": True, "recoverability_reason": "cup stays upright",
             "count": 5, "trial_indices": [1, 2, 4, 5, 7], "episode_ids": [41, 42, 44, 45, 47],
             "example_episode_ids": [41, 42, 44]},
            {"failure_cluster_id": 13, "label": "never approaches the cup", "description": "No motion.",
             "phase": "start", "recoverable_mid_episode": False, "count": 2, "trial_indices": [8, 9],
             "episode_ids": [48, 49], "example_episode_ids": [48, 49]}],
    }
    evidence = {"planner": "TDM-24", "fixture": None,
                "guardrail": {"rule": "R2", "required_type": "recovery_takeover", "why": "5 of 7 fail late",
                              "failures": 7, "no_approach_share": 0.286, "late_share": 0.714},
                "budget_math": {"remaining_cents": 5000, "reset_cents": 200, "needed": 5, "cap_by_settings": 5,
                                "per_episode_cents": {"clean_teleop": 1200, "recovery_takeover": 700},
                                "affordable": {"clean_teleop": 4, "recovery_takeover": 7},
                                "max_amount": {"clean_teleop": 4, "recovery_takeover": 5}},
                "inputs": {"latest_eval": {"eval_run_id": run}, "data_type_override": None,
                           "prices_cents": {"clean_teleop": 1000, "recovery_takeover": 500, "reset": 200}},
                "failure_cluster_ids": [12, 13], "unit_price_cents": 500, "est_cost_incl_resets_cents": 3500,
                "labels_cited": ["drops mid-grasp"],
                "model": {"requested": "claude-opus-5-5", "effort": "high", "served": "claude-opus-5-5",
                          "attempts": [{"message_id": "msg_1", "violations": []}]},
                "guardrail_forced": [], "guardrail_agreed_first_try": True}
    events = [
        ev(1, 0, "web", "ticket.created", {"ticket_id": tid}),
        ev(2, 50, "orchestrator", "eval.finalized", {"eval_run_id": run, "ticket_id": tid, "summary": "3/10"}),
        ev(3, 60, "orchestrator", "planner.failure_cluster", {"failure_cluster_id": 12, "ticket_id": tid, "iteration": 0}),
        ev(4, 61, "orchestrator", "planner.failure_cluster", {"failure_cluster_id": 13, "ticket_id": tid, "iteration": 0}),
        ev(5, 62, "orchestrator.diagnose", "diagnose.clusters", diag_payload),
        ev(6, 90, "orchestrator", "planner.data_plan",
           {"data_plan_id": 5, "ticket_id": tid, "iteration": 0, "reasoning": REASONING, "evidence": evidence}),
        ev(7, 120, "human:web", "spend.approved",
           {"ticket_id": tid, "data_plan_id": 5, "approved_cents": 2500, "approved_by": "human:web", "via": "web"}),
        ev(8, 130, "orchestrator", "task.created", {"task_id": 200, "ticket_id": tid, "data_plan_id": 5}),
        ev(9, 131, "orchestrator", "task.created", {"task_id": 201, "ticket_id": tid, "data_plan_id": 5}),
        ev(10, 1, "web", "ticket.created", {"ticket_id": 99}),  # another ticket: ignored
    ]
    return {
        "ticket": [{"id": tid, "task_description": "Put the cup in the bin", "status": "eval", "iteration": 1,
                    "target_success": 0.8, "budget_cents": 5000, "approved_cents": 2500, "spent_cents": 1400,
                    "created_by": "web", "created_at": T0, "robot_id": 1}],
        "run_settings": [], "review": [],
        "eval_run": [
            {"id": run, "ticket_id": tid, "kind": "baseline", "status": "done", "checkpoint_uri": "pi05_base",
             "training_run_id": 0, "trials": 10, "completed": 10, "successes": 3, "ci_low": 0.108, "ci_high": 0.603,
             "created_at": T0 + 10 * S, "finished_at": T0 + 50 * S},
            {"id": 4, "ticket_id": tid, "kind": "candidate", "status": "done", "checkpoint_uri": "s3://ck/1",
             "training_run_id": 30, "trials": 10, "completed": 10, "successes": 8, "ci_low": 0.49, "ci_high": 0.94,
             "created_at": T0 + 900 * S, "finished_at": T0 + 990 * S}],
        "eval_trial": trials,
        "episode": episodes + bought,
        "failure_cluster": [
            {"id": 12, "ticket_id": tid, "iteration": 0, "label": "drops mid-grasp", "description": "", "count": 5,
             "example_episode_ids": [41, 42, 44], "created_at": T0 + 60 * S},
            {"id": 13, "ticket_id": tid, "iteration": 0, "label": "never approaches the cup", "description": "",
             "count": 2, "example_episode_ids": [48, 49], "created_at": T0 + 61 * S}],
        "data_plan": [{"id": 5, "ticket_id": tid, "iteration": 0, "data_type": "recovery_takeover", "amount": 5,
                       "unit_price_cents": 500, "est_cost_cents": 2500, "reasoning": REASONING,
                       "failure_cluster_ids": [12, 13], "status": "approved", "approved_cents": 2500,
                       "approved_by": "human:web", "approved_at": T0 + 120 * S, "created_at": T0 + 90 * S}],
        "task": [{"id": 200 + k, "ticket_id": tid, "data_plan_id": 5, "type": "recovery_takeover",
                  "status": "completed", "price_cents": 500, "assignee_id": 9, "created_at": T0 + (130 + k) * S,
                  "completed_at": T0 + (300 + k) * S} for k in range(2)]
                + [{"id": 210, "ticket_id": tid, "data_plan_id": 0, "type": "reset", "status": "open", "price_cents": 200,
                    "assignee_id": 0, "created_at": T0 + 140 * S, "completed_at": None}],
        "training_run": [{"id": 30, "ticket_id": tid, "iteration": 0, "episode_ids": [60, 61], "status": "done",
                          "checkpoint_uri": "s3://ck/1", "gpu_cost_cents": 350, "created_at": T0 + 400 * S,
                          "finished_at": T0 + 800 * S}],
        "event_log": events,
    }


def failed_checks(d: dict) -> list[str]:
    return [lk["check"] for lk in d["links"] if lk["ok"] is False]


def test_full_chain_links_by_id():
    out = decision.assemble(fixture(), 7, now_micros=T0)
    assert out["missing"] == []
    [d] = out["decisions"]
    assert d["iteration"] == 0 and d["missing"] == [] and failed_checks(d) == []
    assert d["source_eval"]["eval_run_id"] == 3 and d["source_eval"]["linked_by"] == "diagnose.clusters.eval_run_id"
    assert d["source_eval"]["finalized_event"]["event_id"] == 2
    assert [c["failure_cluster_id"] for c in d["clusters"]] == [12, 13]
    c12 = d["clusters"][0]
    assert c12["phase"] == "lift" and c12["event"]["event_id"] == 3 and c12["description"] == "Cup slips out during lift."
    assert [x["episode_id"] for x in c12["examples"]] == [41, 42, 44]
    assert c12["examples"][0]["trial"] == {"eval_run_id": 3, "trial_index": 1, "success": False,
                                           "failure_note": "dropped cup during lift", "duration_ms": 30000}
    assert c12["examples"][0]["cameras"] == [{"name": "wrist", "uri": "s3://rdf/tickets/7/episodes/41/wrist.mp4"}]
    assert d["diagnosis"]["reasoning"] == "Five failures lose the cup after grasping it."
    p = d["plan"]
    assert p["reasoning"] == REASONING and p["event_reasoning_verbatim"] is True and p["event"]["event_id"] == 6
    assert p["guardrail"]["rule"] == "R2" and p["model"]["served"] == "claude-opus-5-5"
    assert d["alternative"] == {"data_type": "clean_teleop", "unit_price_cents": 1000,
                                "cost_per_episode_incl_reset_cents": 1200, "max_amount": 4, "affordable": 4,
                                "ruled_out_by_rule": "R2", "rule_why": "5 of 7 fail late"}
    assert [t["task_id"] for t in d["tasks"]] == [200, 201]  # the reset (no plan) is not part of the chain
    assert d["tasks"][0]["event"]["event_id"] == 8 and d["tasks"][0]["episode_ids"] == [60]
    assert [e["episode_id"] for e in d["data_bought"]] == [60, 61]
    assert d["training_runs"][0]["training_run_id"] == 30
    assert [e["eval_run_id"] for e in d["after_evals"]] == [4]
    assert d["approvals"][0]["approved_by"] == "human:web"


def test_autonomy_chain_precedes_first_human_action():
    out = decision.assemble(fixture(), 7)
    a = out["decisions"][0]["autonomy"]
    assert out["human_actions"][0]["event_id"] == 7  # spend.approved; ticket.created does not count
    assert a["before_any_human_action"] is True and a["human_actions_in_decision_window"] == []
    assert [x["event_id"] for x in a["chain_events"]] == [2, 5, 3, 4, 6]


def test_human_action_inside_decision_window_is_flagged():
    t = fixture()
    t["event_log"].append(ev(11, 70, "human:web", "human.run_settings_changed", {"ticket_id": 7}))
    d = decision.assemble(t, 7)["decisions"][0]
    assert d["autonomy"]["before_any_human_action"] is False
    assert [h["event_id"] for h in d["autonomy"]["human_actions_in_decision_window"]] == [11]
    assert "no human action between the source eval and the plan" in failed_checks(d)


def test_owner_token_events_are_listed_separately():
    t = fixture()
    t["event_log"].append(ev(12, 80, "owner", "ticket.status_changed", {"ticket_id": 7, "to": "plan_data"}))
    out = decision.assemble(t, 7)
    assert [o["event_id"] for o in out["owner_token_actions"]] == [12]
    assert [o["event_id"] for o in out["decisions"][0]["autonomy"]["owner_token_actions_in_decision_window"]] == [12]


def test_missing_plan_says_so_with_blocked_reason():
    t = fixture()
    t["data_plan"], t["task"], t["training_run"] = [], [], []
    t["event_log"] = [e for e in t["event_log"] if e["type"] not in ("planner.data_plan", "spend.approved", "task.created")]
    t["event_log"].append(ev(13, 95, "planner", "planner.blocked",
                             {"ticket_id": 7, "iteration": 0, "reason": "remaining budget 100c cannot pay"}))
    d = decision.assemble(t, 7)["decisions"][0]
    assert d["plan"] is None and d["alternative"] is None
    assert any("has not recorded a data_plan" in m and "remaining budget 100c" in m for m in d["missing"])


def test_broken_links_are_reported_not_hidden():
    t = fixture()
    t["failure_cluster"][0]["example_episode_ids"] = [40, 41]  # 40 was a success
    t["data_plan"][0]["reasoning"] = REASONING + " edited"
    t["event_log"] = [e for e in t["event_log"] if e["id"] != 5]  # no diagnose.clusters event
    d = decision.assemble(t, 7)["decisions"][0]
    bad = failed_checks(d)
    assert "cluster 12 example episode 40 is a failed trial of the source eval" in bad
    assert "event_log reasoning is the data_plan reasoning verbatim" in bad
    assert any("no diagnose.clusters event" in m for m in d["missing"])
    assert d["source_eval"]["linked_by"] == "example episodes are trials of this run"


def test_empty_ticket_lists_what_is_missing():
    t = fixture()
    t = {"ticket": t["ticket"], "event_log": t["event_log"][:1]}
    out = decision.assemble(t, 7)
    assert out["decisions"] == [] and len(out["missing"]) == 2


class FakeStdb:
    def __init__(self, tables):
        self.tables = tables
        self.queries: list[str] = []

    async def sql(self, q: str) -> list[dict]:
        self.queries.append(q)
        m = re.fullmatch(r"SELECT \* FROM (\w+)(?: WHERE (\w+) = (\d+))?", q)
        assert m, q
        return [copy.deepcopy(r) for r in self.tables.get(m[1], []) if m[2] is None or r.get(m[2]) == int(m[3])]


def _client(tables):
    app = FastAPI()
    app.include_router(decision_router)
    fake = FakeStdb(tables)
    app.dependency_overrides[get_decision_stdb] = lambda: fake
    return TestClient(app), fake


def test_router_returns_chain_and_404():
    c, fake = _client(fixture())
    r = c.get("/tickets/7/decision")
    assert r.status_code == 200
    body = r.json()
    assert body["decisions"][0]["plan"]["data_plan_id"] == 5
    assert "SELECT * FROM eval_trial WHERE eval_run_id = 3" in fake.queries
    assert all(not q.startswith(("INSERT", "UPDATE", "DELETE")) for q in fake.queries)
    assert c.get("/tickets/8/decision").status_code == 404
