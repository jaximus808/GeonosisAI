"""Scripted stand-in for the orchestrator flywheel (TDM-28 harness only).

Until the real flywheel handlers land (ticket state machine, planner, dispatcher, reviewer,
Modal client), ``test_full_loop`` drives the same reducer chain from this script so the fake robot,
the reducers and the stub Modal trainer are exercised end to end:

  created -> baseline (eval via robot_command run_eval) -> diagnose (failure clusters from the
  [FAKE] trial notes) -> plan_data (record_data_plan) -> dispatch (start_teleop on the fake robot)
  -> review (submit_review) -> train (queue_training_run + stub Modal) -> eval (candidate eval)
  -> shipped, or loop back to plan_data while iterations remain.

Every decision it records says ``[FAKE scripted driver]``: it is NOT the planner and its
data_plan rows must never be shown as a real planner decision. It only ever runs against the
harness's throwaway local SpacetimeDB.
"""
from __future__ import annotations

import json
from collections import Counter

from harness import LocalStack, wait_for

TAG = "[FAKE scripted driver]"


class ScriptedFlywheel:
    def __init__(self, st: LocalStack, robot_id: int, *, cmd_timeout: float = 60) -> None:
        self.st, self.robot_id, self.cmd_timeout = st, robot_id, cmd_timeout
        self.c = st.client("orchestrator")

    # ---- helpers
    def status(self, ticket_id: int, to: str, note: str) -> None:
        self.c.call_reducer("set_ticket_status", ticket_id, to, f"{TAG} {note}")

    def command(self, ticket_id: int, type_: str, payload: dict) -> dict:
        before = self.st.newest("robot_command", f"robot_id = {self.robot_id}")
        self.c.call_reducer("issue_robot_command", self.robot_id, type_, json.dumps(payload), ticket_id)
        cmd = wait_for(lambda: (r := self.st.newest("robot_command", f"robot_id = {self.robot_id}"))
                       and (before is None or r["id"] > before["id"]) and r, 10, f"{type_} issued")
        done = wait_for(lambda: (r := self.st.sql(f"SELECT * FROM robot_command WHERE id = {cmd['id']}")[0])
                        ["status"] in ("done", "failed", "preempted") and r, self.cmd_timeout, f"{type_} #{cmd['id']} done")
        if done["status"] != "done":
            raise AssertionError(f"{type_} #{done['id']} ended {done['status']}: {done.get('result_json')}")
        return json.loads(done.get("result_json") or "{}")

    def eval(self, ticket_id: int, checkpoint_uri: str) -> dict:
        from rdf_orchestrator.stats import EvalRunFinalizer

        self.c.call_reducer("start_eval_run", ticket_id, checkpoint_uri, 0)
        run = self.st.newest("eval_run", f"ticket_id = {ticket_id}")
        self.command(ticket_id, "run_eval", {"eval_run_id": run["id"], "checkpoint_uri": checkpoint_uri})
        run = self.st.sql(f"SELECT * FROM eval_run WHERE id = {run['id']}")[0]
        fin = EvalRunFinalizer(lambda **kw: self.c.call_reducer(
            "finalize_eval_run", kw["eval_run_id"], kw["successes"], kw["trials"], kw["ci_low"], kw["ci_high"], kw["summary"]))
        for t in self.st.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {run['id']}"):
            fin.on_eval_trial(t, run)
        return self.st.sql(f"SELECT * FROM eval_run WHERE id = {run['id']}")[0]

    # ---- the loop
    def run(self, ticket_id: int) -> dict:
        settings = self.st.sql(f"SELECT * FROM run_settings WHERE ticket_id = {ticket_id}")[0]
        threshold = float(settings["success_threshold"])
        self.status(ticket_id, "baseline", "robot already onboarded (fake)")
        base = self.eval(ticket_id, "fake://pi05_base")
        self.status(ticket_id, "diagnose", f"baseline {base['successes']}/{base['trials']}")
        trials = self.st.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {base['id']}")
        fails = [t for t in trials if not t["success"]]
        clusters = Counter(t["failure_note"] for t in fails)
        for note, n in clusters.items():
            eps = [t["episode_id"] for t in fails if t["failure_note"] == note and t["episode_id"]][:3]
            self.c.call_reducer("record_failure_cluster", ticket_id, note, n, json.dumps(eps))
        cluster_ids = [r["id"] for r in self.st.sql(f"SELECT * FROM failure_cluster WHERE ticket_id = {ticket_id}")]
        self.status(ticket_id, "plan_data", "clusters recorded")
        candidate = base
        while True:
            ticket = self.st.sql(f"SELECT * FROM ticket WHERE id = {ticket_id}")[0]
            n = int(settings["target_episodes"])
            self.c.call_reducer("record_data_plan", ticket_id, "clean_teleop", n,
                                f"{TAG} not a planner decision: fixed clean_teleop x{n} for the harness",
                                json.dumps({"failure_cluster_ids": cluster_ids, "fake": True}))
            self.status(ticket_id, "dispatch", "fake teleop on the fake robot")
            new_eps = []
            for _ in range(n):
                res = self.command(ticket_id, "start_teleop", {"kind": "clean_teleop"})
                assert res.get("episode_id"), f"fake teleop produced no episode: {res}"
                new_eps.append(res["episode_id"])
            self.status(ticket_id, "review", f"{len(new_eps)} episodes")
            for ep in new_eps:
                self.c.call_reducer("submit_review", ep, True, "", json.dumps({"fake": True, "reviewer": TAG}))
            accepted = [e["id"] for e in self.st.sql(f"SELECT * FROM episode WHERE ticket_id = {ticket_id}")
                        if e["review_status"] == "accepted"]
            self.status(ticket_id, "train", f"{len(accepted)} accepted")
            self.c.call_reducer("queue_training_run", ticket_id, json.dumps(sorted(accepted)), "")
            tr = self.st.newest("training_run", f"ticket_id = {ticket_id}")
            tr = wait_for(lambda: (r := self.st.sql(f"SELECT * FROM training_run WHERE id = {tr['id']}")[0])
                          ["status"] in ("done", "failed") and r, 60, "training run done (stub modal)")
            assert tr["status"] == "done", tr
            self.status(ticket_id, "eval", f"training run {tr['id']} done")
            candidate = self.eval(ticket_id, tr["checkpoint_uri"])
            rate = candidate["successes"] / candidate["trials"]
            if rate >= threshold:
                self.status(ticket_id, "shipped", f"candidate {candidate['successes']}/{candidate['trials']} >= {threshold}")
                return candidate
            if ticket["iteration"] + 1 >= int(settings["max_iterations"]):
                self.status(ticket_id, "failed", "out of iterations")
                return candidate
            self.status(ticket_id, "plan_data", f"candidate {rate:.2f} < {threshold}; another round")
