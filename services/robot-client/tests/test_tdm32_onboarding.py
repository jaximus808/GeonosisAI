"""TDM-32 onboarding checks + the droid's tool (no database: a fake client stands in)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from rdf_robot_client import onboarding as O
from rdf_robot_client import onboarding_tool as T
from rdf_robot_client.adapters.yam import YamArm

CAMS = "services/robot-client/config/cameras.yaml"


def sim_cfg(**over):
    return {"robot": "test.tdm32", "adapter": "sim", "kwargs": {}, "safety_profile": "sim", "safety_overrides": {},
            "cameras_config": CAMS, "rationale": ["sim robot -> SimArm"], **over}


def yam_cfg(sim: bool, **over):
    return {"robot": "test.tdm32", "adapter": "yam", "kwargs": {"sim": sim, "kinematics": "poe"}, "safety_profile": "yam",
            "safety_overrides": {}, "cameras_config": CAMS, "rationale": ["yam"], **over}


# --------------------------------------------------------------------------- tighten-only

def _yam_base():
    return O.load_yaml(O.DEFAULT_SAFETY)["profiles"]["yam"]


@pytest.mark.parametrize("over", [
    {"speed_scale": 0.2},
    {"joints": {"margin_rad": 0.15, "max_velocity": [0.5] * 6, "upper": [3.0, 3.0, 3.0, 1.5, 1.5, 2.0]}},
    {"workspace": {"x": [0.15, 0.45], "z": [0.02, 0.40]}, "watchdog_ms": 150},
    {"gripper": {"max": 0.9, "max_effort": 0.4}},
])
def test_tighten_only_accepts_stricter(over):
    assert O.tighten_only(_yam_base(), over, adapter_limits=YamArm(kinematics="poe").limits) == []


@pytest.mark.parametrize("over,needle", [
    ({"speed_scale": 0.5}, "speed_scale"),
    ({"verified": True}, "verified"),
    ({"joints": {"margin_rad": 0.05}}, "margin_rad"),
    ({"joints": {"max_velocity": [0.8, 0.8, 0.8, 1.2, 1.2, 2.0]}}, "max_velocity[5]"),
    ({"joints": {"lower": [-3.0, 0, 0, -1.6, -1.5, -2.0]}}, "lower[0]"),
    ({"workspace": {"x": [0.05, 0.50]}}, "workspace.x"),
    ({"workspace": {"enabled": False}}, "enabled"),
    ({"watchdog_ms": 0}, "watchdog_ms"),
    ({"gripper": {"max_effort": None}}, "max_effort"),
    ({"unverified_max_speed_scale": 1.0}, "not an overridable"),
])
def test_tighten_only_refuses_looser(over, needle):
    errs = O.tighten_only(_yam_base(), over, adapter_limits=YamArm(kinematics="poe").limits)
    assert errs and any(needle in e for e in errs), errs


def test_check_refuses_loosening_overrides():
    res = O.run_checks(yam_cfg(False, safety_overrides={"joints": {"max_velocity": [2.0] * 6}}))
    assert not res["ok"] and not res["groups"]["safety"]["ok"]
    assert "loosens" in " ".join(res["groups"]["safety"]["errors"])


# --------------------------------------------------------------------------- run_checks

def test_sim_checks_pass_with_live_protocol():
    res = O.run_checks(sim_cfg(), capture_s=1.0)
    assert res["mode"] == "sim" and res["ok"], {k: g["errors"] for k, g in res["groups"].items()}
    c = res["groups"]["contract"]["info"]
    assert "soft e-stop latch + clear" in c["live_steps"] and c["estop_return_ms"] < 50
    assert res["groups"]["cameras"]["info"]["synthetic_capture"]["pass"]
    k = res["groups"]["kinematics"]["info"]
    assert k["ik_success_rate"] >= 0.95 and k["pos_err_max_mm"] < 1.0
    assert res["hil_required"]  # the real-arm part stays listed for the human


def test_hardware_checks_never_connect(monkeypatch):
    def boom(self):
        raise AssertionError("onboarding connected to hardware")

    monkeypatch.setattr(YamArm, "connect", boom)
    res = O.run_checks(yam_cfg(False))
    assert res["mode"] == "hardware" and res["ok"], {k: g["errors"] for k, g in res["groups"].items()}
    assert "skipped" in res["groups"]["contract"]["info"]["live"]
    assert "hardware_probe" in res["groups"] and res["hil_required"]


def test_home_outside_workspace_fails_safety():
    cfg = sim_cfg(safety_overrides={"workspace": {"x": [0.60, 0.70]}})
    g = O.check_safety(cfg)
    assert not g["ok"] and "outside the workspace" in " ".join(g["errors"])


def test_camera_config_without_wrist_fails(tmp_path):
    raw = yaml.safe_load((O.DEFAULT_CAMERAS).read_text())
    raw["cameras"].pop("wrist")
    p = tmp_path / "cams.yaml"
    p.write_text(yaml.safe_dump(raw))
    g = O.check_cameras(sim_cfg(cameras_config=str(p)), live=False)
    assert not g["ok"] and "wrist" in " ".join(g["errors"])


def test_success_check_must_be_operator_decided():
    good = {**T.SUCCESS_TEMPLATE, "ticket_id": 7, "task": "cup in bin", "success_when": ["cup in bin"],
            "failure_when": ["cup outside"]}
    assert O.validate_success_check(good, ticket_id=7) == []
    errs = O.validate_success_check({**good, "decided_by": "classifier", "auto_decide": True}, ticket_id=7)
    assert any("decided_by" in e for e in errs) and any("auto_decide" in e for e in errs)


# --------------------------------------------------------------------------- the tool, against a fake DB

class FakeDB:
    def __init__(self, embodiment="sim_yam", adapter_version=""):
        self.ticket = {"id": 7, "robot_id": 3, "task_description": "put the cup in the bin", "status": "onboarding",
                       "target_success": 0.8}
        self.robot = {"id": 3, "name": "test.tdm32", "embodiment": embodiment, "status": "online",
                      "adapter_version": adapter_version, "camera_config_json": json.dumps({"cameras": {"top": {}, "wrist": {}}}),
                      "last_heartbeat": None}
        self.sessions = [{"id": 11, "ticket_id": 7, "role": "onboarding_droid", "status": "running"}]
        self.events: list[dict] = []
        self.calls: list[tuple] = []

    def sql(self, q):
        if "FROM ticket" in q:
            return [self.ticket]
        if "FROM robot" in q:
            return [self.robot]
        if "FROM agent_session" in q:
            return self.sessions
        if "FROM event_log" in q:
            t = re.search(r"type = '([^']+)'", q).group(1)
            return [e for e in self.events if e["type"] == t]
        raise AssertionError(q)

    def call_reducer(self, name, *args):
        self.calls.append((name, *args))
        if name == "register_robot":
            self.robot = {**self.robot, "adapter_version": args[2], "camera_config_json": args[3]}
        elif name == "append_event":
            self.events.append({"id": len(self.events) + 1, "actor": args[0], "type": args[1], "payload_json": args[2]})


def _ctx(tmp_path, monkeypatch) -> Path:
    monkeypatch.delenv("SPACETIME_HOST", raising=False)
    monkeypatch.delenv("SPACETIME_DB_NAME", raising=False)
    p = tmp_path / "droid_context.json"
    p.write_text(json.dumps({"host": "http://127.0.0.1:1", "db": "x", "ticket_id": 7}))
    return p


def _run(argv, db, capsys):
    rc = T.main(argv, client=db)
    out = json.loads(capsys.readouterr().out)
    return rc, out


def _write_specs(tdir: Path, home):
    (tdir / "success_check.yaml").write_text(yaml.safe_dump({
        **T.SUCCESS_TEMPLATE, "ticket_id": 7, "task": "put the cup in the bin", "start_state": "cup on mark",
        "success_when": ["whole cup inside the bin"], "failure_when": ["cup outside the bin at the time limit"],
        "ambiguous": "failure", "operator_prompt": "Is the cup fully in the bin?"}))
    (tdir / "reset_routine.yaml").write_text(yaml.safe_dump({
        **T.RESET_TEMPLATE, "home_joints": list(home), "human_steps": ["cup back on the start mark"],
        "operator_prompt": "Put the cup back on the mark"}))
    (tdir / "onboarding_report.md").write_text("# Onboarding report\n" + "checked everything. " * 20)


def test_tool_sim_flow_publishes_adapter_version(tmp_path, monkeypatch, capsys):
    ctx = _ctx(tmp_path, monkeypatch)
    db = FakeDB()
    rc, probe = _run(["probe", "--ctx", str(ctx)], db, capsys)
    assert rc == 0 and probe["mode"] == "sim" and probe["agent_session_id"] == 11
    draft = probe["draft_adapter_config"]
    assert draft["adapter"] == "yam" and draft["kwargs"]["sim"] is True
    draft["kwargs"]["kinematics"] = "poe"
    draft["rationale"] = ["sim_yam robot: YamArm on the SDK sim"]
    (tmp_path / "adapter_config.yaml").write_text(yaml.safe_dump(draft))
    rc, out = _run(["publish", "--ctx", str(ctx)], db, capsys)
    assert rc == 1 and "run check first" in out["error"]
    rc, chk = _run(["check", "--ctx", str(ctx)], db, capsys)
    assert rc == 0 and chk["all_ok"], chk
    _write_specs(tmp_path, probe["draft_adapter_facts"]["home_joints"])
    rc, pub = _run(["publish", "--ctx", str(ctx)], db, capsys)
    assert rc == 0 and pub["published"] and pub["status"] == "passed_sim", pub
    assert db.robot["adapter_version"].endswith("+onb.t7")
    onb = json.loads(db.robot["camera_config_json"])["onboarding"]
    assert onb["success_check"]["decided_by"] == "operator" and onb["hil_pending"]
    assert "cameras" in json.loads(db.robot["camera_config_json"])  # existing keys kept
    rep = [e for e in db.events if e["type"] == "onboarding.report"]
    assert rep and rep[0]["actor"] == "droid:onboarding"


def test_tool_refuses_changed_config_and_bad_specs(tmp_path, monkeypatch, capsys):
    ctx = _ctx(tmp_path, monkeypatch)
    db = FakeDB()
    (tmp_path / "adapter_config.yaml").write_text(yaml.safe_dump({**sim_cfg(), "robot": "test.tdm32"}))
    assert _run(["check", "--ctx", str(ctx)], db, capsys)[0] == 0
    _write_specs(tmp_path, (0.0, -1.0, 1.6, 0.0, 0.9, 0.0))
    spec = yaml.safe_load((tmp_path / "success_check.yaml").read_text())
    (tmp_path / "success_check.yaml").write_text(yaml.safe_dump({**spec, "auto_decide": True}))
    rc, out = _run(["publish", "--ctx", str(ctx)], db, capsys)
    assert rc == 1 and "auto_decide" in out["error"] and db.robot["adapter_version"] == ""
    (tmp_path / "adapter_config.yaml").write_text(yaml.safe_dump({**sim_cfg(), "rationale": ["changed"]}))
    rc, out = _run(["publish", "--ctx", str(ctx)], db, capsys)
    assert rc == 1 and "changed since the last check" in out["error"]


def test_tool_hardware_parks_until_human_confirms(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(YamArm, "connect", lambda self: (_ for _ in ()).throw(AssertionError("connected")))
    ctx = _ctx(tmp_path, monkeypatch)
    db = FakeDB(embodiment="i2rt_yam")
    (tmp_path / "adapter_config.yaml").write_text(yaml.safe_dump(yam_cfg(True)))
    rc, out = _run(["check", "--ctx", str(ctx)], db, capsys)
    assert rc == 1 and "hardware" in out["error"]  # a sim config cannot onboard a hardware robot
    (tmp_path / "adapter_config.yaml").write_text(yaml.safe_dump(yam_cfg(False)))
    rc, chk = _run(["check", "--ctx", str(ctx)], db, capsys)
    assert rc == 0 and chk["mode"] == "hardware" and chk["all_ok"]
    from rdf_robot_client.adapters.yam import YAM_READY

    _write_specs(tmp_path, YAM_READY)
    rc, pub = _run(["publish", "--ctx", str(ctx)], db, capsys)
    assert rc == 0 and pub["parked"] and pub["waiting_line"] == T.hil_waiting_line(7)
    assert db.robot["adapter_version"] == "" and any(e["type"] == "onboarding.hil_requested" for e in db.events)
    from rdf_robot_client import onboarding_hil as H

    H.confirm(7, "Jaxon P", notes="CP-1 passed", c=db)
    rc, pub = _run(["publish", "--ctx", str(ctx)], db, capsys)
    assert rc == 0 and pub["published"] and pub["status"] == "passed_hil"
    assert db.robot["adapter_version"].endswith("+onb.t7.hil")
    assert "checklist" in H.checklist(7, c=db).lower()


def test_tool_refuses_other_database(tmp_path, monkeypatch, capsys):
    ctx = _ctx(tmp_path, monkeypatch)
    monkeypatch.setenv("SPACETIME_HOST", "https://maincloud.spacetimedb.com")
    rc, out = _run(["probe", "--ctx", str(ctx)], FakeDB(), capsys)
    assert rc == 1 and "refusing" in out["error"]
