"""TDM-69 interventions against an in-memory mirror + fake reducer writer (no network).

The fake writer applies the reducers ``interventions.py`` (and the TDM-26 dispatcher it uses) call to
the same tables it reads. The test plays the robot and the contributor by writing rows; nothing
here is a real eval result or a real episode.
"""
from __future__ import annotations

import json

import pytest

from rdf_orchestrator import dispatcher as dsp
from rdf_orchestrator import interventions as ivm

NOW = 1_800_000_000.0
TABLES = set(ivm.TABLES) | set(dsp.TABLES)


class FakeDB:
    def __init__(self):
        self.t: dict[str, dict] = {name: {} for name in TABLES}
        self.calls: list[tuple] = []
        self.events: list[tuple[str, dict]] = []
        self.now = NOW

    def us(self, dt: float = 0.0) -> int:
        return int((self.now + dt) * 1e6)

    # mirror
    def rows(self, table):
        return list(self.t.get(table, {}).values())

    def get(self, table, key):
        return self.t.get(table, {}).get(key)

    def put(self, table, **row):
        if "id" not in row:
            row["id"] = max(self.t[table], default=0) + 1
        key = row["ticket_id"] if table == "run_settings" else row["id"]
        self.t[table][key] = row
        return row

    # writer
    def call_reducer(self, name, *args):
        self.calls.append((name, *args))
        getattr(self, f"r_{name}")(*args)

    def r_append_event(self, actor, type_, payload):
        self.events.append((type_, json.loads(payload)))
        self.put("event_log", actor=actor, type=type_, payload_json=payload, ts=self.us())

    def r_create_task(self, ticket_id, type_, price, payload):
        p = json.loads(payload)
        self.put("task", ticket_id=ticket_id, robot_id=self.t["ticket"][ticket_id]["robot_id"],
                 data_plan_id=int(p.get("data_plan_id") or 0), type=type_, status="open",
                 instructions=p.get("instructions", ""), price_cents=price, assignee_id=0, claimed_at=None,
                 lease_expires_at=None, completed_at=None, created_at=self.us())

    def r_cancel_task(self, task_id, reason):
        t = self.t["task"][task_id]
        if t["status"] not in ("open", "claimed"):
            raise RuntimeError(f"task {task_id} is {t['status']}")
        t["status"] = "cancelled"

    def r_complete_task(self, task_id, result):
        t = self.t["task"][task_id]
        if t["status"] != "claimed":
            raise RuntimeError("not claimed")
        t.update(status="completed", completed_at=self.us(), lease_expires_at=None)

    def r_issue_robot_command(self, robot_id, type_, payload, ticket_id):
        if type_ in dsp.JOB_TYPES and any(c["robot_id"] == robot_id and c["status"] in ("issued", "acked")
                                          and c["type"] in dsp.JOB_TYPES for c in self.rows("robot_command")):
            raise RuntimeError("robot busy")
        self.put("robot_command", robot_id=robot_id, ticket_id=ticket_id, type=type_, payload_json=payload,
                 status="issued", result_json=None, issued_at=self.us(), acked_at=None, issued_by="orchestrator")

    # helpers standing in for the robot / contributor
    def cmds(self, type_=None):
        return [c for c in self.rows("robot_command") if type_ is None or c["type"] == type_]

    def ev(self, type_):
        return [p for t, p in self.events if t == type_]

    def claim(self, task_id, contributor_id=7):
        self.t["task"][task_id].update(status="claimed", assignee_id=contributor_id, claimed_at=self.us(),
                                       lease_expires_at=self.us(60))


def _cfg(**kw):
    base = dict(claim_timeout_s=90, takeover_max_s=300, watchdog_s=0, collect=True, robot_stale_s=30)
    base.update(kw)
    return ivm.InterventionConfig(**base)


def _mgr(db, **kw):
    d = dsp.Dispatcher(db, lambda: db, mirror_wait_s=0, clock=lambda: db.now)
    return ivm.InterventionManager(db, lambda: db, _cfg(**kw), clock=lambda: db.now, mirror_wait_s=0, dispatcher=d)


@pytest.fixture
def db():
    db = FakeDB()
    db.put("robot", id=1, name="fake-yam-1", status="online", last_heartbeat=db.us(),
           camera_config_json=json.dumps({"fake": True, "capabilities": ["interventions"]}))
    db.put("ticket", id=1, robot_id=1, status="dispatch", iteration=0, budget_cents=5000, spent_cents=0,
           task_description="put the cup in the bin")
    db.put("run_settings", ticket_id=1, hyperparams_json="{}", eval_trials=10)
    db.put("data_plan", id=1, ticket_id=1, iteration=0, data_type="recovery_takeover", amount=2,
           unit_price_cents=500, approved_cents=1000, status="approved", failure_cluster_ids=[])
    db.put("contributor", id=7, name="[FAKE] c", online=True, kind="contributor")
    return db


def _rollout(db, mgr):
    assert mgr.ensure_collection(1, why="test") == "issued"
    cmd = db.cmds("run_baseline")[-1]
    cmd.update(status="acked", acked_at=db.us())
    return cmd


def _signal(db, mgr, cmd, detector="no_progress"):
    db.put("event_log", actor="robot:1", type=ivm.FAILURE_EVENT, ts=db.us(), payload_json=json.dumps(
        {"robot_id": 1, "ticket_id": 1, "command_id": cmd["id"], "trial_index": 0, "detector": detector,
         "reason": "[FAKE] gripper closed beside the cup"}))
    return mgr.on_signal(db.rows("event_log")[-1])


# --------------------------------------------------------------------------- enablement

def test_defaults_on_for_collection_off_for_evals(db):
    assert ivm.enabled_for(db, 1, "collect")[0] is True
    assert ivm.enabled_for(db, 1, "eval")[0] is False
    assert ivm.enabled_for(db, 1, "baseline")[0] is False
    assert ivm.eval_payload(db, 1, "candidate") == {}
    db.t["run_settings"][1]["hyperparams_json"] = json.dumps({"interventions_enabled": True})
    assert ivm.enabled_for(db, 1, "eval")[0] is True
    db.t["run_settings"][1]["hyperparams_json"] = json.dumps({"interventions_enabled": False})
    assert ivm.enabled_for(db, 1, "collect")[0] is False


def test_collection_needs_robot_capability(db):
    db.t["robot"][1]["camera_config_json"] = json.dumps({"fake": True})
    on, why = ivm.enabled_for(db, 1, "collect")
    assert not on and "capability" in why
    assert _mgr(db).ensure_collection(1) == "disabled"
    assert not db.cmds()


def test_collection_rollout_payload(db):
    mgr = _mgr(db)
    cmd = _rollout(db, mgr)
    p = json.loads(cmd["payload_json"])
    assert p["purpose"] == "collect_recovery" and p["eval_run_id"] == 0 and p["data_plan_id"] == 1
    assert p["interventions"]["enabled"] is True and p["trials"] == 1
    assert mgr.ensure_collection(1) == "robot_busy"  # one rollout at a time (job lock)
    assert db.ev("intervention.collect_rollout")


# --------------------------------------------------------------------------- the takeover path

def test_failure_routes_open_plan_task_pauses_and_pages(db):
    db.put("task", id=10, ticket_id=1, robot_id=1, data_plan_id=1, type="recovery_takeover", status="open",
           instructions="plan task", price_cents=500, assignee_id=0, lease_expires_at=None, completed_at=None)
    mgr = _mgr(db)
    cmd = _rollout(db, mgr)
    assert _signal(db, mgr, cmd) == "started"
    pause = db.cmds("pause_for_takeover")[-1]
    pp = json.loads(pause["payload_json"])
    assert pp["task_id"] == 10 and pp["ref"] == f"iv:1:{cmd['id']}:0"
    st = db.ev("intervention.started")[-1]
    assert st["route"] == "open" and st["task_id"] == 10 and st["detector"] == "no_progress"
    assert db.ev("dispatch.paged")[-1]["task_ids"] == [10]
    assert not [c for c in db.calls if c[0] == "create_task"]
    assert _signal(db, mgr, cmd) == "duplicate"


def test_failure_creates_task_when_none_open(db):
    mgr = _mgr(db)
    cmd = _rollout(db, mgr)
    assert _signal(db, mgr, cmd, detector="timeout") == "started"
    t = [t for t in db.rows("task") if t["type"] == "recovery_takeover"][-1]
    assert dsp.task_ref(t) == f"iv:1:{cmd['id']}:0" and t["price_cents"] == 500 and t["data_plan_id"] == 1
    assert db.ev("intervention.started")[-1]["route"] == "created"


def test_budget_short_declines_and_ends_trial(db):
    db.t["ticket"][1]["budget_cents"] = 300
    mgr = _mgr(db)
    cmd = _rollout(db, mgr)
    assert _signal(db, mgr, cmd).startswith("declined:")
    verdict = json.loads(db.cmds("resume_policy")[-1]["payload_json"])
    assert verdict["end_trial"] is True and verdict["ref"] == f"iv:1:{cmd['id']}:0"
    assert not db.cmds("pause_for_takeover")


def test_eval_failure_declined_by_default(db):
    db.t["ticket"][1]["status"] = "eval"
    db.put("robot_command", id=50, robot_id=1, ticket_id=1, type="run_eval", status="acked", issued_by="orch",
           payload_json=json.dumps({"eval_run_id": 3}), issued_at=db.us(), acked_at=db.us())
    mgr = _mgr(db)
    out = mgr.start(1, detector="operator", reason="Failing button")
    assert out.startswith("declined:interventions disabled")
    assert not db.cmds("resume_policy")  # robot was not waiting (no interventions payload)


def test_full_cycle_claim_handback_episode_review(db):
    mgr = _mgr(db)
    cmd = _rollout(db, mgr)
    _signal(db, mgr, cmd)
    iv = mgr.live_on_robot(1)
    old = dict(db.t["task"][iv.task_id])
    db.now += 12
    db.claim(iv.task_id)
    assert mgr.on_task(db.t["task"][iv.task_id], old) == "claimed"
    assert db.ev("intervention.claimed")[-1]["claim_wait_s"] == 12.0
    db.now += 20
    hb = db.put("robot_command", robot_id=1, ticket_id=1, type="handback", status="done", issued_by="contributor:7",
                payload_json="{}", issued_at=db.us())
    assert mgr.on_command(hb, {**hb, "status": "acked"}) == "handback"
    assert db.ev("intervention.handback")[-1]["takeover_s"] == 20.0
    # the policy finishes the trial; the robot writes the recovery episode and completes the rollout
    ep = db.put("episode", ticket_id=1, robot_id=1, task_id=iv.task_id, contributor_id=7, kind="recovery_takeover",
                review_status="pending", takeover_ts=db.us(-20), handback_ts=db.us(), duration_ms=40000, success=True)
    assert mgr.on_episode(ep) == "recorded"
    old_cmd = dict(cmd)
    cmd["status"] = "done"
    assert mgr.on_command(cmd, old_cmd) == "closed"
    assert db.ev("intervention.closed")[-1]["outcome"] == "recovered"
    assert ("complete_task", iv.task_id) == db.calls[[c[0] for c in db.calls].index("complete_task")][:2]
    reset = [t for t in db.rows("task") if t["type"] == "reset"][-1]
    assert dsp.task_ref(reset) == f"collect:{cmd['id']}"
    # reviewer accepts
    ep_old = dict(ep)
    ep["review_status"] = "accepted"
    assert mgr.on_episode(ep, ep_old) == "reviewed"
    s = mgr.summary(1)
    assert s["count"] == 1 and s["accepted_episodes"] == [ep["id"]] and s["by_outcome"] == {"recovered": 1}


def test_claim_timeout_ends_trial_and_cancels_created_task(db):
    mgr = _mgr(db, claim_timeout_s=30)
    cmd = _rollout(db, mgr)
    _signal(db, mgr, cmd)
    iv = mgr.live_on_robot(1)
    db.now += 10
    mgr.tick()
    assert mgr.live_on_robot(1) is not None
    db.now += 25
    db.t["robot"][1]["last_heartbeat"] = db.us()
    mgr.tick()
    assert mgr.live_on_robot(1) is None
    verdict = json.loads(db.cmds("resume_policy")[-1]["payload_json"])
    assert verdict == {**verdict, "end_trial": True, "outcome": "failure", "ref": iv.ref}
    assert db.t["task"][iv.task_id]["status"] == "cancelled"
    assert db.ev("intervention.timeout")[-1]["stage"] == "claim"
    assert db.ev("intervention.closed")[-1]["outcome"] == "unclaimed_timeout"


def test_takeover_overrun_hands_back(db):
    mgr = _mgr(db, takeover_max_s=60)
    cmd = _rollout(db, mgr)
    _signal(db, mgr, cmd)
    iv = mgr.live_on_robot(1)
    old = dict(db.t["task"][iv.task_id])
    db.claim(iv.task_id)
    mgr.on_task(db.t["task"][iv.task_id], old)
    db.now += 61
    db.t["robot"][1]["last_heartbeat"] = db.us()
    db.t["task"][iv.task_id]["lease_expires_at"] = db.us(30)
    mgr.tick()
    assert json.loads(db.cmds("handback")[-1]["payload_json"])["reason"] == "takeover_overrun"
    assert mgr.live_on_robot(1).phase == "handed_back"


def test_lease_lapse_repages_and_restarts_claim_timer(db):
    mgr = _mgr(db, claim_timeout_s=30)
    cmd = _rollout(db, mgr)
    _signal(db, mgr, cmd)
    iv = mgr.live_on_robot(1)
    old = dict(db.t["task"][iv.task_id])
    db.claim(iv.task_id)
    mgr.on_task(db.t["task"][iv.task_id], old)
    db.now += 70
    old = dict(db.t["task"][iv.task_id])
    db.t["task"][iv.task_id].update(status="open", assignee_id=0, lease_expires_at=None)
    assert mgr.on_task(db.t["task"][iv.task_id], old) == "claim_lapsed"
    assert mgr.live_on_robot(1).claim_deadline_us == db.us(30)


def test_completed_without_handback_hands_back_for_claimant(db):
    mgr = _mgr(db)
    cmd = _rollout(db, mgr)
    _signal(db, mgr, cmd)
    iv = mgr.live_on_robot(1)
    old = dict(db.t["task"][iv.task_id])
    db.claim(iv.task_id)
    mgr.on_task(db.t["task"][iv.task_id], old)
    old = dict(db.t["task"][iv.task_id])
    db.t["task"][iv.task_id]["status"] = "completed"
    assert mgr.on_task(db.t["task"][iv.task_id], old) == "handback_sent"
    assert db.cmds("handback")


def test_restart_replays_trail(db):
    mgr = _mgr(db)
    cmd = _rollout(db, mgr)
    _signal(db, mgr, cmd)
    iv = mgr.live_on_robot(1)
    old = dict(db.t["task"][iv.task_id])
    db.claim(iv.task_id)
    mgr.on_task(db.t["task"][iv.task_id], old)
    fresh = _mgr(db)  # new process: rebuilt from event_log rows
    iv2 = fresh.live_on_robot(1)
    assert iv2 is not None and iv2.ref == iv.ref and iv2.phase == "taken_over" and iv2.contributor_id == 7
    assert _signal(db, fresh, cmd) == "duplicate"


def test_robot_estop_closes_and_cancels(db):
    mgr = _mgr(db)
    cmd = _rollout(db, mgr)
    _signal(db, mgr, cmd)
    iv = mgr.live_on_robot(1)
    db.t["robot"][1]["status"] = "estop"
    mgr.tick()
    assert mgr.live_on_robot(1) is None and db.t["task"][iv.task_id]["status"] == "cancelled"
    assert db.ev("intervention.closed")[-1]["outcome"] == "robot_estop"


def test_collection_stops_at_target(db):
    db.put("task", id=20, ticket_id=1, robot_id=1, data_plan_id=1, type="recovery_takeover", status="completed",
           instructions="", price_cents=500, assignee_id=7)
    db.put("task", id=21, ticket_id=1, robot_id=1, data_plan_id=1, type="recovery_takeover", status="completed",
           instructions="", price_cents=500, assignee_id=7)
    for tid in (20, 21):
        db.put("episode", ticket_id=1, task_id=tid, kind="recovery_takeover", review_status="pending")
    assert _mgr(db).ensure_collection(1) == "target_covered"


def test_robot_event_handback(db):
    mgr = _mgr(db)
    cmd = _rollout(db, mgr)
    _signal(db, mgr, cmd)
    iv = mgr.live_on_robot(1)
    old = dict(db.t["task"][iv.task_id])
    db.claim(iv.task_id)
    mgr.on_task(db.t["task"][iv.task_id], old)
    db.put("event_log", actor="robot:1", type=ivm.HANDBACK_EVENT, ts=db.us(),
           payload_json=json.dumps({"robot_id": 1, "task_id": iv.task_id}))
    assert mgr.on_signal(db.rows("event_log")[-1]) == "handback"
    assert mgr.live_on_robot(1).phase == "handed_back"
