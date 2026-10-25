"""Real tickets on a teleop-only arm (tonight's tdm119_teleop on yam-01): created as a TDM-125 split
(evals on the sim robot, collection on the arm), and no run_eval / run_baseline ever goes to the arm."""
from __future__ import annotations

import json

from rdf_orchestrator import evaluate as evl
from rdf_orchestrator.robot_mode import robot_can_eval, robot_is_teleop_only

import test_tdm67_evaluate as t67
import test_tickets_api as tapi

TELEOP_ONLY = {"sim": False, "teleop": True, "arm": {"x": 1}, "stream": {"url": "https://x"}}
NOTE = ("yam-01 can collect data but can't run evals tonight, so this ticket evaluates on the sim robot "
        "and collects on yam-01.")


def _r(cfg):
    return {"id": 1, "camera_config_json": json.dumps(cfg)}


def test_robot_can_eval_rules():
    assert robot_can_eval(_r({"sim": True}))
    assert robot_can_eval(_r({"sim": True, "teleop": True}))
    assert not robot_can_eval(_r(TELEOP_ONLY))  # missing policy flag + teleop -> teleop-only
    assert not robot_can_eval(_r({**TELEOP_ONLY, "policy": False}))
    assert robot_can_eval(_r({**TELEOP_ONLY, "policy": True}))
    assert robot_can_eval(_r({"cameras": []}))  # an older real client with neither flag keeps evals
    assert robot_is_teleop_only(_r(TELEOP_ONLY)) and not robot_is_teleop_only(None)


class SplitStdb(tapi.FakeStdb):
    async def call_reducer(self, name, *args):
        if name == "append_event":
            self.calls.append((name, *args))
            return
        await super().call_reducer(name, *args)


def _make(robots):
    stdb = SplitStdb()
    for rid, name, status, cfg in robots:
        stdb.tables["robot"].append({"id": rid, "name": name, "status": status, "embodiment": "i2rt_yam",
                                     "camera_config_json": json.dumps(cfg)})
    svc = tapi.TicketsService(stdb, tapi.FakeParser())
    app = tapi.FastAPI()
    app.include_router(tapi.router)
    app.dependency_overrides[tapi.get_tickets_service] = lambda: svc
    return tapi.TestClient(app), stdb


ROBOTS = [(2, "yam-01", "online", TELEOP_ONLY), (5, "sim-other", "online", {"sim": True}),
          (6, "sim-yam-mujoco", "online", {"sim": True})]


def test_real_ticket_on_teleop_only_arm_becomes_a_split():
    client, stdb = _make(ROBOTS)
    r = client.post("/tickets", json={"prompt": "cube on plate, $50", "robot_mode": "real"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["robot"]["name"] == "sim-yam-mujoco" and body["robot_mode"] == "sim"
    assert body["collect_robot"]["name"] == "yam-01"
    assert body["robot_selection"] == "split_auto"
    assert body["note"] == NOTE
    (create,) = [c for c in stdb.calls if c[0] == "create_ticket"]
    assert create[1] == 6
    assert json.loads(create[5])["hyperparams"]["collect_robot_id"] == 2
    ev = {c[2]: json.loads(c[3]) for c in stdb.calls if c[0] == "append_event"}
    assert ev["ticket.split_auto"]["real_robot_name"] == "yam-01" and "teleop-only" in ev["ticket.split_auto"]["reason"]
    assert ev["ticket.collect_robot_set"]["collect_robot_id"] == 2
    # the preview says the same thing before creating
    p = client.post("/tickets/parse", json={"prompt": "cube on plate, $50", "robot_mode": "real"}).json()
    assert p["note"] == NOTE and p["robot"]["name"] == "sim-yam-mujoco" and p["collect_robot"]["name"] == "yam-01"


def test_real_ticket_on_teleop_only_arm_without_a_sim_robot_is_422():
    client, stdb = _make([(2, "yam-01", "online", TELEOP_ONLY), (6, "sim-yam-mujoco", "offline", {"sim": True})])
    r = client.post("/tickets", json={"prompt": "cube on plate, $50", "robot_mode": "real"})
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["code"] == "real_robot_cannot_eval" and "can't run evals tonight" in d["message"]
    assert not [c for c in stdb.calls if c[0] == "create_ticket"]


def test_real_ticket_on_policy_enabled_arm_is_unchanged():
    client, stdb = _make([(2, "yam-01", "online", {**TELEOP_ONLY, "policy": True}),
                          (6, "sim-yam-mujoco", "online", {"sim": True})])
    body = client.post("/tickets", json={"prompt": "cube on plate, $50", "robot_mode": "real"}).json()
    assert body["robot"]["name"] == "yam-01" and body["collect_robot"] is None and body["note"] is None


def test_no_eval_command_ever_goes_to_a_teleop_only_robot():
    db = t67.FakeDB()
    db.put("robot", id=1, name="yam-01", status="online", last_heartbeat=db.us(), camera_config_json=json.dumps(TELEOP_ONLY))
    db.put("ticket", id=1, robot_id=1, status="baseline", iteration=0, task_description="cube on plate")
    db.put("run_settings", ticket_id=1, eval_trials=10)
    e = t67.make(db)
    for _ in range(3):
        assert e.ensure_eval(1, "test") == "robot_cannot_eval"
    e.check_all()
    e.kick_robot(1, "online")
    assert not [c for c in db.rows("robot_command") if c["type"] in evl.EVAL_COMMANDS]
    assert not db.rows("eval_run")  # nothing started, nothing failed: the stage waits
    assert db.t["ticket"][1]["status"] == "baseline"
    assert db.event_types().count("eval.robot_cannot_eval") == 1  # logged once, not every tick
    # an orphaned live run (command missing) is not re-dispatched to the arm either
    db.put("eval_run", ticket_id=1, robot_id=1, training_run_id=0, kind="baseline",
           checkpoint_uri=t67.PI05_BASE_CHECKPOINT, status="running", trials=10, completed=0, successes=0,
           ci_low=0.0, ci_high=0.0, created_at=db.us())
    assert e.ensure_eval(1, "again") == "robot_cannot_eval"
    assert not [c for c in db.rows("robot_command") if c["type"] in evl.EVAL_COMMANDS]
    # restarted with TDM119_ALLOW_POLICY=1 -> policy: true -> evals run there
    db.t["robot"][1]["camera_config_json"] = json.dumps({**TELEOP_ONLY, "policy": True})
    assert e.ensure_eval(1, "policy on") == "dispatched"
