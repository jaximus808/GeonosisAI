"""TDM-119 bring-up CLI + teleop entry, offline: LarpSafeYamArm on a mock of LARP's fork, inside the SafetyLayer."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from rdf_robot_client import tdm119_bringup as bu
from rdf_robot_client.adapters.yam import YAM_READY
from rdf_robot_client.adapters.yam_larp import LARP_YAM_KD, LARP_YAM_KP, LarpSafeYamArm
from rdf_robot_client.safety import SafetyLayer


class Tdm119BringupFakeRobot:
    def __init__(self, q0) -> None:
        self.q = np.array(q0, dtype=float)
        self.closed_with: list[bool] = []
        self.motor_chain = SimpleNamespace(read_states=lambda: [SimpleNamespace(error_code="0x1")] * 7)
        self._kp = np.array((*LARP_YAM_KP, 20.0))
        self._kd = np.array((*LARP_YAM_KD, 0.5))
        self._gripper_limits = np.array([0.1, 4.2])

    def get_observations(self):
        return {"joint_pos": self.q.copy(), "joint_vel": np.zeros(6), "gripper_pos": np.array([0.5])}

    def command_joint_pos(self, cmd) -> None:
        self.q = np.array(cmd[:6], dtype=float)

    def close(self, release_motors: bool = False) -> None:
        self.closed_with.append(release_motors)


@pytest.fixture(autouse=True)
def tdm119_bringup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("YAM_LOCK_DIR", str(tmp_path))
    monkeypatch.delenv("RDF_SAFETY_CONFIG", raising=False)
    monkeypatch.delenv("RDF_SAFETY_SPEED_SCALE", raising=False)
    return tmp_path


def _factory(robot: Tdm119BringupFakeRobot):
    def make():
        inner = LarpSafeYamArm(channel="can_right", robot_factory=lambda _a: robot, kinematics="poe")
        return SafetyLayer(inner, config_path=bu.TDM119_REPO_SAFETY)
    return make


def test_hold_enables_holds_and_releases(capsys):
    robot = Tdm119BringupFakeRobot((0.0, 0.02, 0.03, 0.0, 0.0, 0.0))
    assert bu.hold(1.1, yes=True, arm_factory=_factory(robot)) == 0
    out = capsys.readouterr().out
    assert "YAM_GRIPPER_LIMITS=0.1000,4.2000" in out and "[PASS] hold step" in out
    assert robot.closed_with == [True]


def test_hold_refused_reports_and_never_holds():
    robot = Tdm119BringupFakeRobot((0.0, -0.3, 0.0, 0.0, 0.0, 0.0))  # outside LARP's limits
    assert bu.hold(0.5, yes=True, arm_factory=_factory(robot)) == bu.TDM119_EXIT_REFUSED
    assert robot.closed_with == [True]


def test_jog_out_and_back_then_estop_latches_and_holds(capsys):
    robot = Tdm119BringupFakeRobot(YAM_READY)
    assert bu.jog(dx=0.01, speed=0.02, yes=True, arm_factory=_factory(robot)) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("[")]
    def fields(key: str) -> dict:
        ln = next(ln for ln in lines if key in ln)
        return json.loads(ln[ln.index("{"):])

    est = fields("soft e-stop")
    assert est["latched"] and est["hold_ms"] < bu.TDM119_STOP_BUDGET_MS and est["stop_call_ms"] < 50
    out = fields("+1 cm x")
    assert out["moved_x_m"] == pytest.approx(0.01, abs=0.004)
    assert any("cleared, holding" in ln and ln.startswith("[PASS]") for ln in lines)
    assert robot.closed_with == [True]


def test_jog_refuses_big_or_fast_and_folded_start():
    robot = Tdm119BringupFakeRobot(YAM_READY)
    assert bu.jog(dx=0.10, yes=True, arm_factory=_factory(robot)) == bu.TDM119_EXIT_REFUSED
    assert bu.jog(speed=0.05, yes=True, arm_factory=_factory(robot)) == bu.TDM119_EXIT_REFUSED
    assert robot.closed_with == []  # never enabled
    folded = Tdm119BringupFakeRobot((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    assert bu.jog(dx=0.01, yes=True, arm_factory=_factory(folded)) == 1  # J2/J3 below the safe range: needs --ready
    assert folded.closed_with == [True]


def test_real_arm_needs_go_not_yes():
    with pytest.raises(SystemExit):
        bu.main(["--yes", "hold"])
    assert bu.hold(1.0, yes=False, arm_factory=lambda: pytest.fail("enabled without go")) == bu.TDM119_EXIT_REFUSED


def test_sim_dry_run():
    assert bu.main(["--adapter", "sim", "--yes", "hold", "--seconds", "0.5"]) == 0


def test_sign_off_only_with_the_phrase(tmp_path, monkeypatch):
    dest = tmp_path / "safety.yaml"
    assert bu.sign_off(dest=dest, answer=lambda _p: "yes") == bu.TDM119_EXIT_REFUSED and not dest.exists()
    assert bu.sign_off(dest=dest, answer=lambda _p: bu.SIGN_OFF_PHRASE) == 0
    import yaml

    data = yaml.safe_load(dest.read_text())
    assert data["profiles"]["yam"]["verified"] is True and data["profiles"]["sim"]["verified"] is True
    assert data["profiles"]["yam"]["speed_scale"] == 0.25  # nothing else changed
    monkeypatch.setenv("RDF_SAFETY_CONFIG", str(tmp_path / "missing.yaml"))
    assert bu.verified_check() == bu.TDM119_EXIT_REFUSED   # missing file -> repo copy -> not a sign-off
    monkeypatch.setenv("RDF_SAFETY_CONFIG", str(dest))
    assert bu.verified_check() == 0


def test_preflight_fails_while_vrteleop_runs():
    def fake_run(cmd, **kw):
        if cmd[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="1234 vrteleop run robot.toml\n")
    assert bu.preflight(run=fake_run) == 1


def test_teleop_entry_refuses_policy_and_unsigned(monkeypatch, capsys):
    from rdf_robot_client import tdm119_teleop, teleop
    from rdf_robot_client.tdm118_readonly import tdm118_refuse

    h = tdm119_teleop.tdm119_handlers(allow_policy=False)
    assert h["start_teleop"] is teleop.start_teleop
    assert h["run_baseline"] is tdm118_refuse and h["run_eval"] is tdm118_refuse and h["home"] is tdm118_refuse
    assert "reset_request" not in h and "load_checkpoint" not in h
    assert "run_eval" not in tdm119_teleop.tdm119_handlers(allow_policy=True)
    monkeypatch.setenv("RDF_TELEOP_MAX_LIN_SPEED", "0.10")
    monkeypatch.setenv("RDF_SAFETY_SPEED_SCALE", "0.25")
    assert tdm119_teleop.run(["--check"]) == 0
    out = capsys.readouterr().out
    cfg = json.loads(out[out.index("{"):])
    assert cfg["ee_speed_at_start_m_s"] == pytest.approx(0.05) and cfg["speed_scale"] == 0.25
    assert cfg["adapter_max_joint_speed"] == 0.3 and max(cfg["joint_vmax_rad_s"]) <= 0.3
    monkeypatch.setenv("RDF_TELEOP_MAX_LIN_SPEED", "5")
    assert tdm119_teleop.tdm119_teleop_config().max_lin_speed == 0.25  # capped at the TDM-52 default
    assert tdm119_teleop.run([]) == 3  # no sign-off: refuses before anything opens


def test_northstar_env_files_have_no_inline_comments_or_secrets():
    cfg = Path(__file__).resolve().parents[1] / "config"
    for name in ("tdm118_northstar.env", "tdm119_teleop.env"):
        for ln in (cfg / name).read_text().splitlines():
            if ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                assert "#" not in v and "TOKEN" not in k and "SECRET" not in k, ln
