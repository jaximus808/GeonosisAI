"""TDM-46 YamArm + YAM kinematics.

Runs everywhere: PoE kinematics (numpy) and the adapter against a fake SDK robot.
SDK tests (``i2rt`` importable) cross-check against the SDK's MuJoCo model and drive the SDK's own
MuJoCo SimRobot. Nothing here touches hardware.
"""
from __future__ import annotations

import importlib.util
import math
import threading
import time

import numpy as np
import pytest

from rdf_robot_client.adapters import ArmError, ArmStopped, IKError, Pose, make_adapter
from rdf_robot_client.adapters.base import pose_error as pose_err
from rdf_robot_client.adapters.yam import (
    ACTION_SPACE,
    YAM_READY,
    YamArm,
    move_ee_linear,
    state_dict,
)
from rdf_robot_client.adapters.yam_kinematics import (
    YAM_V1_LOWER,
    YAM_V1_M_MOUNT,
    YAM_V1_UPPER,
    PoeKinematics,
    pose_error,
    tool_offset,
)

HAS_SDK = importlib.util.find_spec("i2rt") is not None
LO, HI = np.array(YAM_V1_LOWER), np.array(YAM_V1_UPPER)


# --------------------------------------------------------------------------- PoE kinematics


def test_poe_home_matches_published_model() -> None:
    k = PoeKinematics("linear_4310")
    t = k.fk(np.zeros(6))
    np.testing.assert_allclose(t, YAM_V1_M_MOUNT @ tool_offset("linear_4310"), atol=1e-12)
    # grasp point at q = 0: 0.1446 m beyond the mount along base +X (tool approach axis = +X)
    np.testing.assert_allclose(t[:3, 3], [0.255248, -0.000039, 0.173404], atol=2e-5)
    np.testing.assert_allclose(t[:3, 2], [1, 0, 0], atol=1e-9)


@pytest.mark.parametrize("gripper", ["linear_4310", "crank_4310", "no_gripper"])
def test_poe_fk_ik_round_trip(gripper: str) -> None:
    k = PoeKinematics(gripper)
    rng = np.random.default_rng(46)
    for q in rng.uniform(LO + 0.2, HI - 0.2, (150, 6)):
        t = k.fk(q)
        seed = np.clip(q + rng.normal(0, 0.08, 6), LO, HI)  # streaming: seed near the answer
        qs = k.ik(t, seed)
        assert np.all(qs >= LO - 1e-9) and np.all(qs <= HI + 1e-9)
        dp, dr = pose_error(t, k.fk(qs))
        assert dp < 2e-4 and dr < 2e-3


def test_poe_jacobian_matches_finite_difference() -> None:
    k = PoeKinematics()
    q = np.array(YAM_READY)
    t0, jac = k._fk_and_jac(q)
    h = 1e-6
    for i in range(6):
        dq = np.zeros(6)
        dq[i] = h
        t1 = k.fk(q + dq)
        np.testing.assert_allclose((t1[:3, 3] - t0[:3, 3]) / h, jac[:3, i], atol=1e-5)


def test_poe_ik_unreachable_raises() -> None:
    k = PoeKinematics()
    t = k.fk(np.array(YAM_READY))
    t[:3, 3] = [1.5, 0.0, 0.2]  # 1.5 m out: beyond reach
    with pytest.raises(IKError):
        k.ik(t, YAM_READY, max_iters=60)


def test_square_around_ready_pose_is_reachable() -> None:
    k = PoeKinematics()
    t_home = k.fk(np.array(YAM_READY))
    np.testing.assert_allclose(t_home[:3, 3], [0.30, 0.0, 0.20], atol=0.01)
    q = np.array(YAM_READY)
    for dx, dy in [(0.1, 0), (0.1, 0.1), (0, 0.1), (0, 0)]:
        t = t_home.copy()
        t[:3, 3] += [dx, dy, 0]
        q = k.ik(t, q)
        assert np.min(np.minimum(q - LO, HI - q)) > 0.3  # well inside every joint range


# --------------------------------------------------------------------------- adapter vs a fake SDK robot


class FakeSdkRobot:
    """Duck-types the bits of i2rt's Robot protocol YamArm uses (teleports to setpoints)."""

    def __init__(self, q0: tuple[float, ...] = (0.0,) * 6, g0: float = 1.0) -> None:
        self.q = np.array(q0, dtype=float)
        self.g = g0
        self.sent: list[tuple[float, np.ndarray]] = []
        self.closed = False
        self.lock = threading.Lock()

    def get_observations(self) -> dict[str, np.ndarray]:
        with self.lock:
            return {"joint_pos": self.q.copy(), "joint_vel": np.zeros(6), "gripper_pos": np.array([self.g])}

    def command_joint_pos(self, cmd: np.ndarray) -> None:
        assert cmd.shape == (7,)
        with self.lock:
            self.sent.append((time.monotonic(), cmd.copy()))
            self.q = cmd[:6].copy()
            self.g = float(cmd[6])

    def close(self) -> None:
        self.closed = True


def _arm(smoothing: bool = False, q0: tuple[float, ...] = YAM_READY, g0: float = 1.0) -> tuple[YamArm, FakeSdkRobot]:
    fake = FakeSdkRobot(q0, g0)
    arm = YamArm(robot_factory=lambda _a: fake, kinematics="poe", smoothing=smoothing)
    return arm, fake


def test_make_adapter_yam_without_hardware() -> None:
    # sim = stock i2rt 120c3c8 limits; on hardware "yam" is LarpSafeYamArm with LARP's (tests/test_tdm119_yam_larp.py)
    arm = make_adapter("yam", kinematics="poe", sim=True)
    assert isinstance(arm, YamArm) and arm.name == "yam" and arm.dof == 6
    assert arm.limits.lower == YAM_V1_LOWER and arm.limits.upper == YAM_V1_UPPER
    assert arm.sdk_clip_limits.lower[1] == pytest.approx(-0.15)
    assert ACTION_SPACE["action"][-1] == "gripper" and len(ACTION_SPACE["action"]) == 7
    assert arm.describe()["joint_limits"]["lower"] == list(YAM_V1_LOWER)


@pytest.mark.skipif(HAS_SDK, reason="SDK installed here")
def test_connect_without_sdk_explains() -> None:
    with pytest.raises(ArmError, match="I2RT SDK is not installed"):
        YamArm(kinematics="poe").connect()


def test_connect_holds_boot_pose_and_keeps_gripper() -> None:
    arm, fake = _arm(q0=(0.1, 0.5, 0.4, 0.0, 0.0, 0.0), g0=0.3)
    arm.connect()
    assert len(fake.sent) == 1
    np.testing.assert_allclose(fake.sent[0][1], [0.1, 0.5, 0.4, 0, 0, 0, 0.3])
    st = arm.status()
    assert st.ok and st.holding
    arm.disconnect()
    assert fake.closed and not arm.status().connected


def test_command_joints_gripper_and_clip() -> None:
    arm, fake = _arm()
    arm.connect()
    arm.command_joints([0.0, 1.0, 1.0, -0.5, 0.1, 0.2], gripper=1.7)
    np.testing.assert_allclose(fake.sent[-1][1], [0.0, 1.0, 1.0, -0.5, 0.1, 0.2, 1.0])
    arm.command_gripper(-0.2)
    assert fake.sent[-1][1][6] == 0.0
    # beyond the SDK's own clip (URDF - 0.15): we mirror its clip in last_joint_cmd
    arm.command_joints([0.0, -1.0, 1.0, 0.0, 0.0, 0.0])
    assert arm.last_joint_cmd[1] == pytest.approx(-0.15)
    with pytest.raises(ArmError):
        arm.command_joints([0.0] * 5)
    with pytest.raises(ArmError):
        arm.command_joints([0.0, math.nan, 0, 0, 0, 0])
    arm.disconnect()


def test_hold_and_stop_keep_gripper_command_and_latch() -> None:
    arm, fake = _arm()
    arm.connect()
    arm.command_joints(YAM_READY, gripper=0.0)  # closed on an object
    fake.g = 0.25  # measured opening: blocked by the object
    arm.hold()
    assert fake.sent[-1][1][6] == 0.0  # still squeezing
    arm.stop()
    assert arm.status().estopped and arm.last_stop_at is not None
    assert fake.sent[-1][1][6] == 0.0
    with pytest.raises(ArmStopped):
        arm.command_joints(YAM_READY)
    with pytest.raises(ArmStopped):
        arm.command_gripper(1.0)
    arm.clear_stop()
    arm.command_joints(YAM_READY)
    arm.disconnect()


def test_stop_is_fast_and_thread_safe() -> None:
    arm, _ = _arm(smoothing=True)
    arm.connect()
    stop_streaming = threading.Event()
    errors: list[BaseException] = []

    def stream() -> None:
        q = list(YAM_READY)
        while not stop_streaming.is_set():
            q[0] += 0.001
            try:
                arm.command_joints(q)
            except ArmStopped:
                return
            except BaseException as e:  # noqa: BLE001
                errors.append(e)
                return
            time.sleep(1 / 30)

    th = threading.Thread(target=stream)
    th.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    arm.stop()
    assert time.monotonic() - t0 < 0.05
    th.join(timeout=1)
    stop_streaming.set()
    assert not errors and arm.status().estopped
    arm.disconnect()


def test_smoothing_interpolates_between_commands() -> None:
    arm, fake = _arm(smoothing=True)
    arm.connect()
    start = np.array(YAM_READY)
    goal = start + np.array([0.03, 0, 0, 0, 0, 0])
    arm.command_joints(goal)
    time.sleep(0.15)
    j1 = [c[1][0] for c in fake.sent[1:]]
    assert len(j1) >= 5  # servo thread re-sent at ~200 Hz
    assert all(b >= a - 1e-12 for a, b in zip(j1, j1[1:]))  # monotone ramp, no overshoot
    assert j1[0] < goal[0] - 1e-3 and j1[-1] == pytest.approx(goal[0])
    assert arm.status().detail["servo_hz_avg"] > 50
    arm.disconnect()


def test_move_ee_linear_is_a_straight_line() -> None:
    arm, _ = _arm()
    arm.connect()
    a = arm.ee_pose()
    b = a.translated((0.05, 0.03, 0.0))
    pts: list[tuple[float, ...]] = []
    assert move_ee_linear(arm, b, 0.3, on_tick=lambda _t, q: pts.append(arm.fk(q).xyz))
    pa, pb = np.array(a.xyz), np.array(b.xyz)
    d = (pb - pa) / np.linalg.norm(pb - pa)
    for p in pts:
        v = np.array(p) - pa
        assert np.linalg.norm(v - d * v.dot(d)) < 5e-4  # off-line distance
    assert pose_err(b, arm.ee_pose())[0] < 5e-4
    s = state_dict(arm)
    assert set(s) == {"joint_pos", "joint_vel", "ee_pose", "gripper", "ts"}
    arm.disconnect()


# --------------------------------------------------------------------------- against the real SDK (no hardware)


@pytest.mark.skipif(not HAS_SDK, reason="I2RT SDK (i2rt) not installed")
def test_sdk_model_agrees_with_poe() -> None:
    from rdf_robot_client.adapters.yam_kinematics import SdkKinematics

    rng = np.random.default_rng(7)
    for gripper in ("linear_4310", "linear_3507", "crank_4310", "flexible_4310", "no_gripper"):
        sdk, poe = SdkKinematics("yam", gripper), PoeKinematics(gripper)
        for q in rng.uniform(LO, HI, (100, 6)):
            dp, dr = pose_error(sdk.fk(q), poe.fk(q))
            assert dp < 1e-5 and dr < 1e-5, gripper


@pytest.mark.skipif(not HAS_SDK, reason="I2RT SDK (i2rt) not installed")
def test_sdk_sim_robot_round_trip() -> None:
    """[SIM] YamArm on the SDK's MuJoCo SimRobot (sim=True): not hardware."""
    arm = YamArm(sim=True, kinematics="sdk", smoothing=False)
    assert arm.embodiment == "sim_yam" and arm.describe()["sim"] is True
    arm.connect()
    try:
        assert arm.status().ok
        arm.command_joints(YAM_READY, gripper=0.5)
        s = arm.read_state()
        np.testing.assert_allclose(s.joints, YAM_READY, atol=1e-9)
        assert s.gripper == pytest.approx(0.5)
        target = s.ee_pose.translated((0.05, -0.05, 0.03))
        q = arm.ik(target)
        assert max(pose_err(target, arm.fk(q))) < 2e-3
        arm.command_joints(q)
        assert pose_err(target, arm.ee_pose())[0] < 2e-4
    finally:
        arm.disconnect()


def test_pose_helpers_consistent() -> None:
    k = PoeKinematics()
    t = k.fk(np.array(YAM_READY))
    p = Pose.from_matrix(t.tolist())
    np.testing.assert_allclose(np.array(p.matrix()), t, atol=1e-9)
