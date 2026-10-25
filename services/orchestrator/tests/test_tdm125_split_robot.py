"""TDM-125: split-robot tickets. Baseline/eval on the ticket's robot (sim), collection on
run_settings hyperparams.collect_robot_id (e.g. the real yam-01). No network: in-memory mirror +
a fake writer whose create_task honours payload.robot_id like the TDM-125 reducer."""
from __future__ import annotations

import json

import pytest

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator import job_drop_channels as chans
from rdf_orchestrator import planner as P
from rdf_orchestrator import report
from rdf_orchestrator.robot_mode import collect_capabilities, collect_robot_id, is_split
from rdf_orchestrator.stdb import Row

SIM_A, REAL_B = 1, 2


@pytest.fixture(autouse=True)
def _tdm125_no_page_hook():
    saved = dsp._page_hook
    dsp.set_page_hook(None)
    yield
    dsp.set_page_hook(saved)


def tdm125_robot(rid, name, *, sim=False, status="online"):
    return {"id": rid, "name": name, "status": status, "embodiment": "yam", "last_heartbeat": 1,
            "camera_config_json": json.dumps({"sim": True} if sim else {"cameras": []})}


class Tdm125DB:
    """Mirror (rows/get) + writer (call_reducer) for the dispatcher."""

    def __init__(self):
        self.t: dict[str, dict] = {n: {} for n in dsp.TABLES}
        self.events: list[tuple[str, dict]] = []
        self.calls: list[tuple] = []

    def rows(self, table):
        return list(self.t.get(table, {}).values())

    def get(self, table, key):
        return self.t.get(table, {}).get(key)

    def put(self, table, **row):
        row.setdefault("id", max(self.t[table], default=0) + 1)
        self.t[table][row["ticket_id"] if table == "run_settings" else row["id"]] = row
        return row

    def call_reducer(self, name, *args):
        self.calls.append((name, *args))
        if name == "append_event":
            self.events.append((args[1], json.loads(args[2])))
        elif name == "create_task":
            tid, typ, price, payload = args
            p = json.loads(payload)
            rid = int(p.get("robot_id") or 0) or self.t["ticket"][tid]["robot_id"]  # = the TDM-125 reducer
            self.put("task", ticket_id=tid, robot_id=rid, data_plan_id=p.get("data_plan_id") or 0, type=typ,
                     status="open", instructions=p.get("instructions", ""), price_cents=price, assignee_id=0,
                     completed_at=None)
        elif name == "issue_robot_command":
            rid, typ, payload, tid = args
            self.put("robot_command", robot_id=rid, ticket_id=tid, type=typ, payload_json=payload, status="issued")
        elif name == "cancel_task":
            self.t["task"][args[0]]["status"] = "cancelled"

    def ev(self, type_):
        return [p for t, p in self.events if t == type_]


def tdm125_setup(collect=REAL_B, b_status="online"):
    db = Tdm125DB()
    db.put("robot", **tdm125_robot(SIM_A, "sim-yam-mujoco", sim=True))
    db.put("robot", **tdm125_robot(REAL_B, "yam-01", status=b_status))
    db.put("ticket", id=7, robot_id=SIM_A, task_description="put the cube on the plate", budget_cents=10_000,
           spent_cents=0, approved_cents=0, status="dispatch", iteration=0)
    db.put("run_settings", ticket_id=7, hyperparams_json=json.dumps({"prompt": "cube on plate", "collect_robot_id": collect}))
    db.put("data_plan", id=3, ticket_id=7, iteration=0, data_type="clean_teleop", amount=2, unit_price_cents=1000,
           est_cost_cents=2000, status="approved", approved_cents=2000, failure_cluster_ids=[])
    return db, dsp.Dispatcher(db, lambda: db, mirror_wait_s=0.2)


# --------------------------------------------------------------------------- helpers

def test_collect_robot_id_and_split():
    t = {"robot_id": SIM_A}
    assert collect_robot_id({"hyperparams_json": "{}"}, t) == SIM_A
    assert collect_robot_id({"hyperparams_json": '{"collect_robot_id": 2}'}, t) == REAL_B
    assert is_split({"hyperparams_json": '{"collect_robot_id": 2}'}, t)
    assert not is_split({"hyperparams_json": '{"collect_robot_id": 1}'}, t)
    assert not is_split({"hyperparams_json": '{"collect_robot_id": 0}'}, t)
    cap = collect_capabilities(tdm125_robot(REAL_B, "yam-01"), tdm125_robot(SIM_A, "sim", sim=True))
    assert cap["clean_only"] and cap["data_types"] == ["clean_teleop"] and cap["resets"] == "in_person"
    same = collect_capabilities(tdm125_robot(SIM_A, "sim", sim=True), tdm125_robot(SIM_A, "sim", sim=True))
    assert not same["clean_only"]


# --------------------------------------------------------------------------- routing

def test_collection_tasks_and_teleop_go_to_the_collect_robot():
    db, d = tdm125_setup()
    st = d.dispatch_plan(3)
    tasks = [t for t in db.rows("task") if t["data_plan_id"] == 3]
    assert len(tasks) == 2 and st.created_task_ids
    assert {t["robot_id"] for t in tasks} == {REAL_B}  # the real robot row, not the sim eval robot
    assert not db.ev("dispatch.collect_robot_unsupported")
    task = tasks[0]
    task.update(status="claimed", assignee_id=55)
    assert d.on_teleop_task(task, {"status": "open"}) == "issued"
    (cmd,) = [c for c in db.rows("robot_command") if c["type"] == "start_teleop"]
    assert cmd["robot_id"] == REAL_B


def test_eval_resets_stay_on_the_eval_robot_and_pause_resets_follow_the_collect_robot():
    db, d = tdm125_setup()
    db.put("eval_run", id=9, ticket_id=7, robot_id=SIM_A, status="running", kind="candidate")
    r = d.on_eval_trial({"id": 1, "eval_run_id": 9, "trial_index": 0, "success": False, "created_at": 0})
    assert r is not None and db.get("task", r.task_id)["robot_id"] == SIM_A
    r2 = d.on_robot_paused(db.get("robot", REAL_B))
    assert r2 is not None and r2.created
    assert db.get("task", r2.task_id)["robot_id"] == REAL_B and db.get("task", r2.task_id)["type"] == "reset"


def test_offline_collect_robot_waits():
    db, d = tdm125_setup(b_status="offline")
    st = d.dispatch_plan(3)
    assert st.state == "waiting_for_robot" and st.reason == "Waiting for yam-01 (offline)"
    assert not [t for t in db.rows("task") if t["data_plan_id"] == 3]
    (ev,) = db.ev("dispatch.waiting_for_robot")
    assert ev["robot_id"] == REAL_B
    db.get("robot", REAL_B)["status"] = "online"  # it comes back: the plan dispatches on it
    st = d.dispatch_plan(3)
    assert st.state == "collecting" and {t["robot_id"] for t in db.rows("task")} == {REAL_B}


def test_old_module_ignoring_payload_robot_id_is_flagged():
    db, d = tdm125_setup()
    orig = db.call_reducer

    def old_module(name, *args):
        if name == "create_task":
            p = json.loads(args[3])
            p.pop("robot_id", None)
            args = (*args[:3], json.dumps(p))
        return orig(name, *args)

    db.call_reducer = old_module
    d.dispatch_plan(3)
    (ev,) = db.ev("dispatch.collect_robot_unsupported")
    assert ev["wanted_robot_id"] == REAL_B


# --------------------------------------------------------------------------- planner

def _tdm125_planner_tables(collect):
    clusters = [Row(id=11, ticket_id=7, iteration=0, label="drops mid-grasp", description="", count=6,
                    example_episode_ids=[], created_at=1)]
    return {
        "ticket": [Row(id=7, robot_id=SIM_A, task_description="cube", target_success=0.8, budget_cents=5000,
                       approved_cents=0, spent_cents=0, status="plan_data", iteration=0)],
        "robot": [Row(tdm125_robot(SIM_A, "sim-yam-mujoco", sim=True)), Row(tdm125_robot(REAL_B, "yam-01"))],
        "run_settings": [Row(ticket_id=7, eval_trials=10, success_threshold=0.8, require_ci_low=False, budget_cents=5000,
                             target_episodes=5, max_iterations=3, data_type_override="recovery_takeover",
                             hyperparams_json=json.dumps({"collect_robot_id": collect}))],
        "eval_run": [Row(id=3, ticket_id=7, robot_id=SIM_A, training_run_id=0, kind="baseline", checkpoint_uri="b",
                         status="done", trials=10, completed=10, successes=4, ci_low=0.1, ci_high=0.6,
                         created_at=1, finished_at=2)],
        "failure_cluster": clusters,
    }


class _Tdm125Claude:
    def __init__(self, data_type):
        from types import SimpleNamespace

        self.calls = []
        a = {"data_type": data_type, "amount": 3, "reasoning": 'Fix "drops mid-grasp".', "failure_cluster_ids": [11],
             "consider_dataset_offer": False, "offer_max_price_cents": 0}

        def create(**kw):
            self.calls.append(kw)
            return SimpleNamespace(id="m", model=P.MODEL, stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                   content=[SimpleNamespace(type="text", text=json.dumps(a))])

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_planner_is_constrained_to_clean_demos_on_a_split_ticket():
    tbl = _tdm125_planner_tables(REAL_B)
    inp = P.gather_inputs(lambda n: tbl.get(n, []), tbl["ticket"][0])
    assert inp.collect_robot["clean_only"] and inp.collect_robot["robot_name"] == "yam-01"
    g = P.guardrail(inp)  # late failures (R2) and a recovery override both lose to the collect robot
    assert (g.rule, g.required_type) == ("collect_robot", "clean_teleop")
    out = P.plan(inp, _Tdm125Claude("recovery_takeover"))
    assert out.data_type == "clean_teleop" and "[guardrail]" in out.reasoning
    assert out.evidence["inputs"]["collect_robot"]["robot_mode"] == "real"


def test_planner_unchanged_without_a_split():
    tbl = _tdm125_planner_tables(0)
    inp = P.gather_inputs(lambda n: tbl.get(n, []), tbl["ticket"][0])
    assert inp.collect_robot is None and P.guardrail(inp).rule == "override"


# --------------------------------------------------------------------------- labels + report

def test_report_says_real_arm_data_and_sim_evals():
    t = {
        "ticket": [{"id": 7, "robot_id": SIM_A, "status": "eval", "iteration": 0, "task_description": "cube",
                    "target_success": 0.8, "budget_cents": 1000, "approved_cents": 0, "spent_cents": 0,
                    "created_at": 1, "created_by": "web"}],
        "robot": [tdm125_robot(SIM_A, "sim-yam-mujoco", sim=True), tdm125_robot(REAL_B, "yam-01")],
        "run_settings": [{"ticket_id": 7, "success_threshold": 0.8, "require_ci_low": False, "created_at": 1,
                          "hyperparams_json": json.dumps({"collect_robot_id": REAL_B})}],
        "eval_run": [{"id": 10, "ticket_id": 7, "robot_id": SIM_A, "kind": "baseline", "status": "done", "successes": 3,
                      "trials": 10, "completed": 10, "ci_low": 0.1, "ci_high": 0.6, "training_run_id": 0,
                      "checkpoint_uri": "base", "created_at": 2}],
        "eval_trial": [],
        "episode": [{"id": i, "ticket_id": 7, "robot_id": REAL_B, "kind": "clean_teleop", "review_status": "accepted",
                     "created_at": 3} for i in (1, 2, 3)],
        "event_log": [],
    }
    r = report.assemble(t, 7, now_micros=10)
    sp = r["robot_mode"]["split"]
    assert sp["line"] == "Data: 3 real-arm episodes (yam-01) · Evals: sim robot"
    assert r["robot_mode"]["by_mode"]["real"]["episodes"] == 3 and r["robot_mode"]["by_mode"]["real"]["trials"] == 0
    md = report.render_markdown(r)
    assert "Data: 3 real-arm episodes (yam-01)" in md and "not real-arm success rates" in md


def test_job_drop_headline_says_which_arm():
    p = chans.DropPost(task_id=1, kind="Clean demo", title="t", instructions="", price="$10.00", price_note="",
                       minutes=3, robot="yam-01", where="From your phone", link="", arm="real robot arm")
    assert "Clean demo on the real robot arm" in chans.headline(p)
    assert " on the " not in chans.headline(chans.DropPost(task_id=1, kind="Reset", title="t", instructions="",
                                                          price="$2", price_note="", minutes=1, robot="r",
                                                          where="At the robot", link=""))
