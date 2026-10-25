"""TDM-114 robot arbitration against the TDM-69 in-memory mirror + fake writer (no network).

The live situation it reproduces: two sim tickets on one sim robot. Ticket 1 is collecting (a
recovery_takeover plan) and its rollout is paused for a takeover nobody claimed; ticket 2 is in
baseline and has no eval_run because the robot is busy. The test plays the robot by writing rows.
"""
from __future__ import annotations

import json

import pytest

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator import evaluate as evl
from rdf_orchestrator import interventions as ivm
from rdf_orchestrator import robot_arbitration as arb
from test_tdm69_interventions import FakeDB, _cfg  # noqa: E402 - sibling test module (pytest prepends tests/)


class Tdm114DB(FakeDB):
    def __init__(self):
        super().__init__()
        for t in set(evl.TABLES) | {"training_run"}:
            self.t.setdefault(t, {})

    def r_start_eval_run(self, ticket_id, ckpt, trials):
        tk = self.t["ticket"][ticket_id]
        self.put("eval_run", ticket_id=ticket_id, robot_id=tk["robot_id"], training_run_id=0, kind="baseline",
                 checkpoint_uri=ckpt, status="running", trials=trials or 10, completed=0, successes=0,
                 created_at=self.us())


def tdm114_setup():
    db = Tdm114DB()
    db.put("robot", id=1, name="sim-yam-mujoco", status="online", last_heartbeat=db.us(),
           camera_config_json=json.dumps({"sim": True, "capabilities": ["interventions"]}))
    for tid, status in ((1, "dispatch"), (2, "baseline")):
        db.put("ticket", id=tid, robot_id=1, status=status, iteration=0, budget_cents=5000, spent_cents=0,
               task_description="put the cube on the plate")
        db.put("run_settings", ticket_id=tid, hyperparams_json="{}", eval_trials=10)
    db.put("data_plan", id=1, ticket_id=1, iteration=0, data_type="recovery_takeover", amount=2,
           unit_price_cents=500, approved_cents=1000, status="approved", failure_cluster_ids=[])
    db.put("task", id=12, ticket_id=1, robot_id=1, data_plan_id=1, type="recovery_takeover", status="open",
           instructions="plan task", price_cents=500, assignee_id=0, lease_expires_at=None, completed_at=None)
    d = dsp.Dispatcher(db, lambda: db, mirror_wait_s=0, clock=lambda: db.now)
    mgr = ivm.InterventionManager(db, lambda: db, _cfg(), clock=lambda: db.now, mirror_wait_s=0, dispatcher=d)
    ev = evl.EvalOrchestrator(db, lambda: db, evl.EvalConfig(watchdog_s=0), clock=lambda: db.now,
                              activate=lambda *a: None, mirror_wait_s=0)
    a = arb.Arbiter(db, lambda: db, clock=lambda: db.now, ivm=mgr, ev=ev, timeout_s=120)
    return db, mgr, ev, a


def tdm114_pause(db, mgr):
    """Ticket 2 enters baseline AFTER ticket 1's rollout started: issue the rollout with ticket 2 parked."""
    db.t["ticket"][2]["status"] = "diagnose"
    assert mgr.ensure_collection(1, why="test") == "issued"
    db.t["ticket"][2]["status"] = "baseline"
    cmd = db.cmds("run_baseline")[-1]
    cmd.update(status="acked", acked_at=db.us())
    db.put("event_log", actor="robot:1", type=ivm.FAILURE_EVENT, ts=db.us(), payload_json=json.dumps(
        {"robot_id": 1, "ticket_id": 1, "command_id": cmd["id"], "trial_index": 0, "detector": "timeout",
         "reason": "[SIM] trial ran 45s"}))
    assert mgr.on_signal(db.rows("event_log")[-1]) == "started"
    db.t["robot"][1]["status"] = "paused"
    return cmd


@pytest.fixture
def stack():
    return tdm114_setup()


def test_holder_says_paused_for_takeover(stack):
    db, mgr, ev, a = stack
    cmd = tdm114_pause(db, mgr)
    h = arb.holder(db, 1)
    assert h["ticket_id"] == 1 and h["command_id"] == cmd["id"] and h["kind"] == "collect"
    assert h["state"] == "paused_for_takeover" and h["task_id"] == 12 and not h["human"]
    assert ev.ensure_eval(2) in ("robot_busy", "waiting_for_robot")
    q = arb.robot_queue(db, 1)
    assert q["holder"]["ticket_id"] == 1 and [x["ticket_id"] for x in q["queue"]] == [2]


def test_claim_now_preempts_paused_rollout_holds_and_runs_eval(stack):
    db, mgr, ev, a = stack
    cmd = tdm114_pause(db, mgr)
    res = a.claim_now(2, by="op")
    assert res["outcome"] == "preempted" and res["preempted"]["task_outcome"] == "returned"
    verdict = json.loads(db.cmds("resume_policy")[-1]["payload_json"])
    assert verdict["end_trial"] is True
    assert db.t["task"][12]["status"] == "open"  # back on the marketplace
    assert db.ev("intervention.closed")[-1]["outcome"] == "preempted"
    assert db.ev(arb.REASSIGNED)[-1]["to_ticket_id"] == 2 and db.ev(arb.CLAIM_NOW)
    assert arb.hold_for(db, 1) is not None
    # the robot ends the trial; the rollout command closes; the collect reset runs and completes
    cmd.update(status="done")
    mgr.on_command(cmd, {"status": "acked"})
    for c in db.cmds():
        if c["status"] in ("issued", "acked") and c["type"] in dsp.JOB_TYPES:
            c["status"] = "done"
    for t in db.rows("task"):
        if t["type"] == "reset" and t["status"] == "open":
            t["status"] = "cancelled"
    db.t["robot"][1].update(status="online", last_heartbeat=db.us())
    # ticket 1 must not grab the robot back; ticket 2's baseline starts
    assert mgr.ensure_collection(1) == "robot_reassigned"
    assert ev.ensure_eval(2, why="kick") == "dispatched"
    assert db.cmds("run_eval")[-1]["ticket_id"] == 2
    # baseline done -> ticket 2 leaves baseline -> hold released
    db.t["ticket"][2]["status"] = "diagnose"
    assert arb.hold_for(db, 1) is None


def test_auto_yield_after_timeout(stack):
    db, mgr, ev, a = stack
    tdm114_pause(db, mgr)
    assert a.tick() == []  # not yet
    db.now += 121
    db.t["robot"][1]["last_heartbeat"] = db.us()
    out = a.tick()
    assert len(out) == 1 and out[0]["auto"] is True and out[0]["to_ticket_id"] == 2
    assert db.ev(arb.YIELDED) and arb.hold_for(db, 1)["auto"] is True
    assert db.t["task"][12]["status"] == "open"


def test_auto_yield_off_and_no_queue(stack):
    db, mgr, ev, a = stack
    tdm114_pause(db, mgr)
    db.t["ticket"][2]["status"] = "diagnose"  # nobody waiting
    db.now += 500
    assert a.tick() == []
    a.timeout_s = 0
    db.t["ticket"][2]["status"] = "baseline"
    assert a.tick() == []


def test_fifo_collection_yields_to_waiting_eval(stack):
    db, mgr, ev, a = stack
    assert mgr.ensure_collection(1) == "yield_to_eval"
    assert not db.cmds("run_baseline")


def test_claim_now_refuses_person_driving(stack):
    db, mgr, ev, a = stack
    tdm114_pause(db, mgr)
    db.claim(12)
    mgr.on_task(db.t["task"][12], {"status": "open"})
    with pytest.raises(arb.ArbitrationError) as e:
        a.claim_now(2)
    assert e.value.status == 409 and e.value.code == "person_driving"


def test_resume_ends_hold(stack):
    db, mgr, ev, a = stack
    tdm114_pause(db, mgr)
    a.claim_now(2)
    out = a.resume(1, by="op")
    assert out["outcome"] == "resumed" and arb.hold_for(db, 1) is None
    with pytest.raises(arb.ArbitrationError):
        a.resume(1)


def test_cancelled_ticket_releases_robot(stack):
    db, mgr, ev, a = stack
    tdm114_pause(db, mgr)
    db.t["ticket"][1]["status"] = "cancelled"
    out = a.release_ticket(1, why="is cancelled")
    assert out and db.ev(arb.RELEASED) and db.ev("intervention.closed")[-1]["outcome"] == "preempted"
    assert mgr.ensure_collection(1) == "not_collecting"


def test_clear_estop_sim_only(stack):
    db, mgr, ev, a = stack
    db.t["robot"][1]["status"] = "estop"
    assert a.clear_sim_estop(1, by="op")["requested"] is True
    assert db.ev(arb.ESTOP_CLEAR)[-1]["robot_id"] == 1
    db.t["robot"][1]["camera_config_json"] = json.dumps({"capabilities": []})
    with pytest.raises(arb.ArbitrationError) as e:
        a.clear_sim_estop(1, by="op")
    assert e.value.code == "real_robot"
