"""LarpSafeYamArm (docs/real-arm-safety.md), offline: a mock of LARP's i2rt fork, no hardware, no CAN."""
from __future__ import annotations

import ast
import fcntl
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from rdf_robot_client.adapters import make_adapter, yam_larp
from rdf_robot_client.adapters.base import ArmError
from rdf_robot_client.adapters.yam_larp import (
    LARP_YAM_KD,
    LARP_YAM_KP,
    LARP_YAM_LOWER,
    LARP_YAM_UPPER,
    MAX_JOINT_SPEED,
    LarpSafeYamArm,
    larp_lock_path,
)

REST = (0.0, 0.02, 0.03, 0.0, 0.0, 0.0)
SRC = Path(__file__).resolve().parents[1] / "src" / "rdf_robot_client"


class Tdm119Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Tdm119MotorInfo:
    def __init__(self, error_code: str = "0x1") -> None:
        self.error_code = error_code


class Tdm119FakeChain:
    def __init__(self, codes: list[str]) -> None:
        self.codes = codes

    def read_states(self) -> list[Tdm119MotorInfo]:
        return [Tdm119MotorInfo(c) for c in self.codes]


class Tdm119FakeForkRobot:
    """Duck-types LARP's MotorChainRobot: teleports, records sends and how it was closed."""

    def __init__(self, q0=REST, g0=0.0, codes=None, kp=None, kd=None) -> None:
        self.q = np.array(q0, dtype=float)
        self.g = g0
        self.sent: list[np.ndarray] = []
        self.closed_with: list[bool] = []
        self.motor_chain = Tdm119FakeChain(codes or ["0x1"] * 7)
        self._kp = np.array(kp if kp is not None else (*LARP_YAM_KP, 20.0))
        self._kd = np.array(kd if kd is not None else (*LARP_YAM_KD, 0.5))

    def get_observations(self):
        return {"joint_pos": self.q.copy(), "joint_vel": np.zeros(6), "gripper_pos": np.array([self.g])}

    def command_joint_pos(self, cmd) -> None:
        self.sent.append(np.array(cmd, dtype=float))
        self.q = np.array(cmd[:6], dtype=float)

    def close(self, release_motors: bool = False) -> None:
        self.closed_with.append(release_motors)


@pytest.fixture(autouse=True)
def tdm119_env(tmp_path, monkeypatch):
    monkeypatch.setenv("YAM_LOCK_DIR", str(tmp_path))
    monkeypatch.delenv("YAM_SIM", raising=False)
    monkeypatch.delenv("YAM_MAX_JOINT_SPEED", raising=False)
    monkeypatch.delenv("YAM_ALLOW_GRIPPER_CALIBRATION", raising=False)
    monkeypatch.delenv("YAM_GRIPPER_LIMITS", raising=False)
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    c = Tdm119Clock()
    monkeypatch.setattr("time.monotonic", c)
    return c


def _arm(robot: Tdm119FakeForkRobot, **kw) -> LarpSafeYamArm:
    return LarpSafeYamArm(channel="can_right", robot_factory=lambda _a: robot, kinematics="poe", servo_thread=False, **kw)


# ------------------------------------------------------------------------------------------- registry / static


def test_make_adapter_yam_is_larp_safe_on_hardware_and_stock_in_sim():
    arm = make_adapter("yam", channel="can_right", kinematics="poe")
    assert isinstance(arm, LarpSafeYamArm) and arm.real and arm.smoothing
    assert arm.limits.lower == LARP_YAM_LOWER and arm.limits.upper == LARP_YAM_UPPER
    assert arm.max_joint_speed == MAX_JOINT_SPEED
    sim = make_adapter("yam", sim=True, kinematics="poe")
    assert not sim.real and sim.limits.upper[0] == pytest.approx(3.14159, abs=1e-4)


def test_speed_can_only_go_lower_and_auto_recovery_is_refused(monkeypatch):
    monkeypatch.setenv("YAM_MAX_JOINT_SPEED", "2.0")
    assert LarpSafeYamArm(kinematics="poe").max_joint_speed == MAX_JOINT_SPEED
    assert LarpSafeYamArm(kinematics="poe", max_joint_speed=0.1).max_joint_speed == 0.1
    with pytest.raises(ArmError, match="auto_recovery"):
        LarpSafeYamArm(kinematics="poe", enable_auto_recovery=True)


def test_safety_box_sits_inside_larp_limits():
    from rdf_robot_client.safety import SafetyLayer

    lim = SafetyLayer(LarpSafeYamArm(kinematics="poe"), start_watchdog=False).limits
    assert all(lo > a for lo, a in zip(lim.lower, LARP_YAM_LOWER))
    assert all(hi < b for hi, b in zip(lim.upper, LARP_YAM_UPPER))


# ------------------------------------------------------------------------------------------- connect


def test_enable_holds_the_measured_pose(clock):
    robot = Tdm119FakeForkRobot(q0=(0.1, 0.4, 0.5, -0.2, 0.0, 0.3))
    arm = _arm(robot)
    arm.connect()
    np.testing.assert_allclose(robot.sent[0][:6], robot.q)
    assert len(robot.sent) == 1 and arm.status().holding
    arm.disconnect()


def test_disconnect_releases_the_larp_way_and_unlocks(clock, tdm119_env):
    robot = Tdm119FakeForkRobot()
    arm = _arm(robot)
    arm.connect()
    arm.disconnect()
    assert robot.closed_with == [True]  # damp until settled, then disable (the fork's close)
    fd = os.open(larp_lock_path("can_right"), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # free again
    os.close(fd)


def test_refuses_a_bus_someone_else_holds(clock):
    path = larp_lock_path("can_right")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.pwrite(fd, b"4242\n", 0)
    built = []
    arm = LarpSafeYamArm(channel="can_right", robot_factory=lambda _a: built.append(1), kinematics="poe", servo_thread=False)
    try:
        with pytest.raises(ArmError, match="pid 4242"):
            arm.connect()
        assert built == []  # the SDK was never opened
    finally:
        os.close(fd)


@pytest.mark.parametrize("robot,match", [
    (Tdm119FakeForkRobot(q0=(0.0, -0.2, 0.0, 0.0, 0.0, 0.0)), "outside LARP's limits"),
    (Tdm119FakeForkRobot(q0=(3.2, 0.5, 0.5, 0.0, 0.0, 0.0)), "outside LARP's limits"),
    (Tdm119FakeForkRobot(codes=["0x1"] * 6 + ["0xD"]), "motor fault"),
    (Tdm119FakeForkRobot(kp=(80, 80, 80, 10, 10, 10, 20)), "not LARP's"),
])
def test_refuses_to_start_and_releases(clock, robot, match):
    arm = _arm(robot)
    with pytest.raises(ArmError, match=match):
        arm.connect()
    assert robot.closed_with == [True] and not arm.status().connected
    arm2 = _arm(Tdm119FakeForkRobot())  # the lock was released
    arm2.connect()
    arm2.disconnect()


# ------------------------------------------------------------------------------------------- the ramp


def _drive(arm: LarpSafeYamArm, clock: Tdm119Clock, robot: Tdm119FakeForkRobot, seconds: float) -> None:
    for _ in range(int(seconds * arm.servo_rate_hz)):
        clock.t += 1.0 / arm.servo_rate_hz
        arm._servo_tick()


def _max_speed(robot: Tdm119FakeForkRobot, dt: float) -> float:
    q = np.array([s[:6] for s in robot.sent])
    return float(np.max(np.abs(np.diff(q, axis=0)))) / dt


def test_first_absolute_target_is_ramped_at_most_0p3_rad_s(clock):
    robot = Tdm119FakeForkRobot()
    arm = _arm(robot)
    arm.connect()
    goal = np.array([0.5, 1.0, 0.9, -0.6, 0.2, 0.0])
    arm.command_joints(goal)                 # e.g. a fixed home pose or a raw first policy action
    assert len(robot.sent) == 1               # nothing jumps on the command itself
    _drive(arm, clock, robot, 0.5)
    assert _max_speed(robot, 1.0 / arm.servo_rate_hz) <= MAX_JOINT_SPEED + 1e-6
    assert float(np.max(np.abs(robot.sent[-1][:6] - REST))) == pytest.approx(0.15, abs=0.01)  # 0.3 rad/s * 0.5 s
    _drive(arm, clock, robot, 6.0)
    np.testing.assert_allclose(robot.sent[-1][:6], goal, atol=1e-6)
    assert arm.rate_limited > 0
    arm.disconnect()


def test_after_hold_the_next_command_ramps_from_the_measured_pose(clock):
    robot = Tdm119FakeForkRobot()
    arm = _arm(robot)
    arm.connect()
    arm.command_joints(REST)
    _drive(arm, clock, robot, 0.1)
    robot.q = np.array([0.0, 0.3, 0.3, 0.0, 0.0, 0.0])  # the arm sagged / was moved by hand
    arm.hold()
    np.testing.assert_allclose(robot.sent[-1][:6], robot.q)  # hold = the measured pose
    n = len(robot.sent)
    arm.command_joints([0.0, 1.2, 1.2, 0.0, 0.0, 0.0])      # first policy action after a pause
    _drive(arm, clock, robot, 0.2)
    q = np.array([s[:6] for s in robot.sent[n - 1:]])
    assert float(np.max(np.abs(np.diff(q, axis=0)))) * arm.servo_rate_hz <= MAX_JOINT_SPEED + 1e-6
    arm.disconnect()


def test_move_joints_duration_respects_the_cap(clock, monkeypatch):
    robot = Tdm119FakeForkRobot()
    arm = _arm(robot)
    arm.connect()
    seen = {}

    def fake_move(self, target, **kw):
        seen.update(kw)
        return True

    monkeypatch.setattr(yam_larp.YamArm, "move_joints", fake_move)
    arm.move_joints([0.6, 0.02, 0.03, 0.0, 0.0, 0.0])
    assert seen["duration"] >= 1.5 * 0.6 / MAX_JOINT_SPEED - 1e-9
    arm.disconnect()


# ------------------------------------------------------------------------------------------- the fork


def _install_fake_fork(monkeypatch, *, version="1.1.1", source="", close_has_release=True):
    calls: list[dict] = []

    class ArmType:
        @staticmethod
        def from_string_name(n):
            return f"arm:{n}"

    class GripperType:
        @staticmethod
        def from_string_name(n):
            return f"grip:{n}"

    def get_yam_robot(channel="can0", arm_type=None, gripper_type=None, zero_gravity_mode=True,
                      gripper_limits_override=None, sim=False, reset_startup_communication_fault=False):
        calls.append({"channel": channel, "zero_gravity_mode": zero_gravity_mode, "sim": sim,
                          "gripper_limits_override": gripper_limits_override,
                          "reset_startup_communication_fault": reset_startup_communication_fault})
        return Tdm119FakeForkRobot()

    class MotorChainRobot:
        if close_has_release:
            def close(self, release_motors: bool = False): ...
        else:
            def close(self): ...

    gr = types.ModuleType("i2rt.robots.get_robot")
    gr.get_yam_robot = get_yam_robot
    mcr = types.ModuleType("i2rt.robots.motor_chain_robot")
    mcr.MotorChainRobot = MotorChainRobot
    ut = types.ModuleType("i2rt.robots.utils")
    ut.ArmType, ut.GripperType = ArmType, GripperType
    robots = types.ModuleType("i2rt.robots")
    robots.get_robot, robots.motor_chain_robot, robots.utils = gr, mcr, ut
    root = types.ModuleType("i2rt")
    root.robots = robots
    for name, mod in {"i2rt": root, "i2rt.robots": robots, "i2rt.robots.get_robot": gr,
                      "i2rt.robots.motor_chain_robot": mcr, "i2rt.robots.utils": ut}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr("importlib.metadata.version", lambda n: version if n == "i2rt" else "0")
    monkeypatch.setattr(yam_larp.inspect, "getsource", lambda obj: source)
    return calls


def test_real_connect_uses_the_fork_with_zero_gravity_off(clock, monkeypatch):
    calls = _install_fake_fork(monkeypatch)
    arm = LarpSafeYamArm(channel="can_right", kinematics="poe", gripper_limits=(0.0, 4.0), servo_thread=False)
    arm.connect()
    assert calls == [{"channel": "can_right", "zero_gravity_mode": False, "sim": False,
                          "gripper_limits_override": pytest.approx(np.array([0.0, 4.0])),
                          "reset_startup_communication_fault": False}]
    assert arm.fork_version == "1.1.1"
    arm.disconnect()


@pytest.mark.parametrize("kw,match", [
    ({"version": "1.1.2"}, "not LARP's fork"),
    ({"source": "def get_yam_robot(check_motor_config=True): ..."}, "check_motor_config"),
    ({"close_has_release": False}, "release_motors"),
])
def test_stock_i2rt_is_refused(clock, monkeypatch, kw, match):
    calls = _install_fake_fork(monkeypatch, **kw)
    arm = LarpSafeYamArm(channel="can_right", kinematics="poe", gripper_limits=(0.0, 4.0), servo_thread=False)
    with pytest.raises(ArmError, match=match):
        arm.connect()
    assert calls == []


def test_no_surprise_gripper_calibration(clock, monkeypatch):
    calls = _install_fake_fork(monkeypatch)
    arm = LarpSafeYamArm(channel="can_right", kinematics="poe", servo_thread=False)
    with pytest.raises(ArmError, match="calibration"):
        arm.connect()
    assert calls == []


# ------------------------------------------------------------------------------------------- never-do


FORBIDDEN_NAMES = {"save_zero_position", "set_zero", "set_timeout", "write_register", "write_param",
                   "save_params", "save_param", "motor_check", "check_motor_types", "clean_error", "set_zero_position"}


def test_no_zeroing_or_register_writes_anywhere_in_the_robot_client():
    """Fails if robot-client code references a zero / register-write / flash / error-clear path
    (identifiers, attributes, imports or keyword arguments; comments and docstrings don't count),
    or passes check_motor_config=True."""
    hits = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.alias):
                names = [node.name.split(".")[-1], *(node.asname and [node.asname] or [])]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = node.module.split(".")
            elif isinstance(node, ast.keyword) and node.arg:
                names = [node.arg]
                if node.arg == "check_motor_config" and not (isinstance(node.value, ast.Constant) and node.value.value is False):
                    hits.append(f"{path.name}:{node.lineno} check_motor_config not False")
            hits += [f"{path.name}:{getattr(node, 'lineno', '?')} {n}" for n in names if n in FORBIDDEN_NAMES]
    assert hits == []
