"""YamArm (TDM-46): the I2RT YAM arm through the official I2RT Python SDK.

SDK: https://github.com/i2rt-robotics/i2rt (not on PyPI; pinned/tested at commit 120c3c8, package
version 1.1.2, repo tag v1.3.6). See ``services/robot-client/docs/yam-sdk-notes.md`` for install,
CAN setup and the bring-up checklist. The SDK is imported lazily in ``connect()``, so this module
imports (and ``make_adapter("yam")`` constructs) on laptops without it.

What the SDK gives us and how we use it:
  * ``i2rt.robots.get_robot.get_yam_robot(channel, arm_type, gripper_type, zero_gravity_mode=False)``
    returns a ``MotorChainRobot``: a background thread runs the DM motors in MIT mode over CAN
    (PD on position + MuJoCo gravity-compensation torque), ~250 Hz target. ``command_joint_pos(q7)``
    just replaces the PD setpoint (6 arm joints in rad + the gripper normalised 0 = closed .. 1 =
    open, the same convention as our adapter). ``zero_gravity_mode=False`` makes it hold the pose
    it booted in instead of going limp.
  * ``sim=True`` returns the SDK's MuJoCo ``SimRobot`` (same protocol, teleports to setpoints, no
    dynamics). ``YamArm(sim=True)`` uses it as a laptop stand-in; everything is labelled sim.
  * Kinematics: the SDK's MuJoCo/mink IK on its arm+gripper model (``yam_kinematics.SdkKinematics``),
    falling back to our numpy PoE model of the same published geometry when the SDK is missing.

Control model: callers stream ``command_joints`` / ``command_ee`` at ``control_rate_hz`` (30 Hz,
the policy rate). With ``smoothing`` on (default) a servo thread re-sends a linearly interpolated
setpoint to the SDK at ``servo_rate_hz`` (200 Hz) between consecutive commands, so the 80 Nm/rad PD
sees a ramp rather than a 30 Hz staircase (cost: one control period of latency). Off: every
command goes straight to the SDK.

Limits: we add none of our own (the safety layer, TDM-45, wraps this adapter). ``limits`` are the
URDF joint ranges; ``sdk_clip_limits`` are what the SDK itself clips commands to (URDF +/- 0.15 rad);
``last_joint_cmd`` mirrors that clip so the recorder logs what the driver actually received.

Hold/stop keep the GRIPPER at its last commanded value (holding it at the measured opening would
drop the squeeze on a grasped object); only the arm joints freeze at the measured position.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from .base import (
    ArmAdapter,
    ArmError,
    ArmNotConnected,
    ArmState,
    ArmStatus,
    ArmStopped,
    IKError,
    JointLimits,
    Pose,
)
from .yam_kinematics import (
    SDK_CLIP_BUFFER,
    YAM_JOINT_NAMES,
    YAM_V1_LIMITS,
    make_kinematics,
)

log = logging.getLogger("rdf_robot_client.yam")

# Ready pose: tool pitched 45 deg down, grasp point at (0.30, 0, 0.20) m in the base frame, far from
# every joint limit (>= 0.8 rad) over a +/-5 cm square around it. Computed from the official model;
# TO VERIFY ON ARM (zero offsets, table height). q = 0 is the folded rest pose the arm powers up in
# (joint2/joint3 sit on their lower limits there).
YAM_READY = (0.0, 0.98, 0.93, -0.73, 0.0, 0.0)
YAM_REST = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

# Action-space convention we record and train on (TDM-25 episode format, CONTEXT.md "save both"):
#   action   = joint_pos_target[6] (rad, joint1..joint6, after safety) + gripper target[1] (0..1)  -> 7 floats
#   ee_target = commanded tool pose (xyz m + wxyz quat, base frame) stored alongside in
#               ik_target_pos / ik_target_quat (FK of the joint target for joint-space commands).
#   observation.state = measured joints[6] + measured gripper[1] (ArmState.state_vector()).
ACTION_SPACE = {
    "action": [*YAM_JOINT_NAMES, "gripper"],
    "action_units": "rad x6, gripper 0=closed..1=open",
    "ee_target": "ik_target_pos (m, base frame) + ik_target_quat (w,x,y,z)",
    "state": [*YAM_JOINT_NAMES, "gripper"],
}


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _env_limits(name: str) -> tuple[float, float] | None:
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    lo, hi = (float(x) for x in v.split(","))
    return (lo, hi)


class YamArm(ArmAdapter):
    """I2RT YAM (6 DOF + gripper) via the I2RT SDK. See the module docstring."""

    name = "yam"
    embodiment = "i2rt_yam"
    adapter_version = "yam-tdm46-i2rt@120c3c8"
    dof = 6
    control_rate_hz = 30.0

    def __init__(
        self,
        *,
        channel: str | None = None,
        arm_type: str | None = None,
        gripper_type: str | None = None,
        sim: bool | None = None,
        kinematics: str | None = None,
        gripper_limits: Sequence[float] | None = None,
        smoothing: bool | None = None,
        servo_rate_hz: float = 200.0,
        control_rate_hz: float | None = None,
        home: Sequence[float] | None = None,
        enable_auto_recovery: bool = False,
        robot_factory: Callable[[YamArm], Any] | None = None,
    ) -> None:
        """All args default from the environment (``YAM_*``, see docs/yam-sdk-notes.md) so
        ``make_adapter("yam")`` with no kwargs works on the Jetson.

        ``robot_factory(self) -> Robot`` replaces the SDK constructor (tests inject a fake).
        """
        self.channel = channel or os.environ.get("YAM_CAN_CHANNEL", "can0")
        self.arm_type = arm_type or os.environ.get("YAM_ARM_TYPE", "yam")
        self.gripper_type = gripper_type or os.environ.get("YAM_GRIPPER", "linear_4310")
        self.sim = _env_bool("YAM_SIM", False) if sim is None else bool(sim)
        self.gripper_limits = tuple(gripper_limits) if gripper_limits is not None else _env_limits("YAM_GRIPPER_LIMITS")
        self.smoothing = _env_bool("YAM_SMOOTHING", True) if smoothing is None else bool(smoothing)
        self.servo_rate_hz = float(servo_rate_hz)
        if control_rate_hz is not None:
            self.control_rate_hz = float(control_rate_hz)
        self.enable_auto_recovery = enable_auto_recovery
        self._robot_factory = robot_factory
        self.has_gripper = self.gripper_type not in ("no_gripper", "yam_teaching_handle")

        if self.arm_type != "yam":
            log.warning("arm_type %s: limits/home below are the YAM v1 values; check them", self.arm_type)
        self.limits: JointLimits = YAM_V1_LIMITS
        self.sdk_clip_limits = JointLimits(
            lower=tuple(v - SDK_CLIP_BUFFER for v in self.limits.lower),
            upper=tuple(v + SDK_CLIP_BUFFER for v in self.limits.upper),
            max_velocity=self.limits.max_velocity,
        )
        self.home_joints = tuple(home) if home is not None else YAM_READY
        self.rest_joints = YAM_REST
        self.kin = make_kinematics(kinematics or os.environ.get("YAM_KINEMATICS", "auto"),
                                   arm_type=self.arm_type, gripper=self.gripper_type if self.has_gripper else "no_gripper",
                                   limits=self.limits)
        if self.sim:
            self.embodiment = "sim_yam"
            self.adapter_version = "yam-tdm46-i2rt-sim@120c3c8"

        self._robot: Any = None
        self._lock = threading.RLock()
        self._estopped = False
        self._holding = True
        self._fault: str | None = None
        # servo interpolation state: segment from _seg_from to _seg_to over [_seg_t0, _seg_t0 + _seg_dur]
        self._seg_from = np.zeros(self.dof)
        self._seg_to = np.zeros(self.dof)
        self._seg_t0 = 0.0
        self._seg_dur = 1.0 / self.control_rate_hz
        self._last_cmd_t: float | None = None
        self._gripper_cmd = 1.0
        self._servo_stop = threading.Event()
        self._servo_thread: threading.Thread | None = None
        self._servo_ticks = 0
        self._servo_t0 = 0.0
        self.commands = 0
        self.ik_failures = 0
        self.last_stop_at: float | None = None

    # ------------------------------------------------------------------ static facts
    @property
    def joint_names(self) -> list[str]:
        return list(YAM_JOINT_NAMES)

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(), "sim": self.sim, "sdk": "i2rt@120c3c8 (github i2rt-robotics/i2rt)",
            "arm_type": self.arm_type, "gripper_type": self.gripper_type, "can_channel": None if self.sim else self.channel,
            "kinematics": self.kin.backend, "tool_point": "grasp_site", "smoothing": self.smoothing,
            "servo_rate_hz": self.servo_rate_hz if self.smoothing else None,
            "joint_limits": {"lower": list(self.limits.lower), "upper": list(self.limits.upper),
                             "source": "yam.urdf / yam.xml (SDK clips commands at +/-0.15 rad beyond)"},
            "action_space": ACTION_SPACE,
            **({"note": "SDK MuJoCo SimRobot stand-in, NOT hardware"} if self.sim else {}),
        }

    # ------------------------------------------------------------------ lifecycle
    def _make_robot(self) -> Any:
        if self._robot_factory is not None:
            return self._robot_factory(self)
        try:
            from i2rt.robots.get_robot import get_yam_robot
            from i2rt.robots.utils import ArmType, GripperType
        except ImportError as e:
            raise ArmError(
                "the I2RT SDK is not installed in this environment (pip package 'i2rt' from "
                "github.com/i2rt-robotics/i2rt); see services/robot-client/docs/yam-sdk-notes.md"
            ) from e
        kw: dict[str, Any] = {}
        if self.gripper_limits is not None and self.has_gripper:
            kw["gripper_limits_override"] = np.array(self.gripper_limits, dtype=float)
        log.info("connecting YAM: channel=%s arm=%s gripper=%s sim=%s", self.channel, self.arm_type,
                 self.gripper_type, self.sim)
        return get_yam_robot(
            channel=self.channel,
            arm_type=ArmType.from_string_name(self.arm_type),
            gripper_type=GripperType.from_string_name(self.gripper_type),
            zero_gravity_mode=False,  # hold the boot pose with PD instead of going limp
            sim=self.sim,
            enable_auto_recovery=self.enable_auto_recovery,
            **kw,
        )

    def connect(self) -> None:
        with self._lock:
            if self._robot is not None:
                return
            try:
                robot = self._make_robot()
            except ArmError:
                raise
            except Exception as e:  # CAN down, motor errors, joint-limit check at boot ...
                raise ArmError(f"YAM connect failed: {type(e).__name__}: {e}") from e
            self._robot = robot
            self._fault = None
            q, g = self._measured()
            self._seg_from = q.copy()
            self._seg_to = q.copy()
            self._seg_t0 = time.monotonic()
            self._gripper_cmd = g if self.has_gripper else 0.0
            self._holding = True
            self._send(q)
            if self.smoothing:
                self._servo_stop.clear()
                self._servo_ticks = 0
                self._servo_t0 = time.monotonic()
                self._servo_thread = threading.Thread(target=self._servo_loop, name="yam-servo", daemon=True)
                self._servo_thread.start()

    def disconnect(self) -> None:
        """Hold, stop the servo thread and close the SDK.

        On the real arm, closing stops the CAN stream; the motors hit their 400 ms timeout and go
        to DAMPING, i.e. the arm sinks slowly under gravity. Move to a supported pose first
        (``move_joints(arm.rest_joints)``).
        """
        thread = None
        with self._lock:
            if self._robot is None:
                return
            try:
                self._hold_locked()
            except Exception:
                log.exception("YAM hold before disconnect failed")
            self._servo_stop.set()
            thread, self._servo_thread = self._servo_thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        with self._lock:
            robot, self._robot = self._robot, None
        try:
            robot.close()
        except Exception:
            log.exception("YAM SDK close failed")

    # ------------------------------------------------------------------ SDK I/O helpers
    def _require(self) -> Any:
        if self._robot is None:
            raise ArmNotConnected("YAM is not connected")
        return self._robot

    def _measured(self) -> tuple[np.ndarray, float]:
        obs = self._require().get_observations()
        q = np.array(obs["joint_pos"], dtype=float)[: self.dof]
        g = float(np.asarray(obs["gripper_pos"]).reshape(-1)[0]) if self.has_gripper and "gripper_pos" in obs else 0.0
        return q, g

    def _send(self, q: np.ndarray) -> None:
        """One setpoint to the SDK (it copies under its own lock; we hand it a fresh array)."""
        q = np.clip(q, self.sdk_clip_limits.lower, self.sdk_clip_limits.upper)
        cmd = np.empty(self.dof + (1 if self.has_gripper else 0))
        cmd[: self.dof] = q
        if self.has_gripper:
            cmd[self.dof] = self._gripper_cmd
        self._robot.command_joint_pos(cmd)

    def _setpoint_now(self, now: float) -> np.ndarray:
        if self._seg_dur <= 0:
            return self._seg_to
        a = min(max((now - self._seg_t0) / self._seg_dur, 0.0), 1.0)
        return self._seg_from + (self._seg_to - self._seg_from) * a

    def _servo_loop(self) -> None:
        period = 1.0 / self.servo_rate_hz
        nxt = time.monotonic()
        while not self._servo_stop.is_set():
            with self._lock:
                if self._robot is None:
                    return
                if not self._holding and not self._estopped:
                    try:
                        self._send(self._setpoint_now(time.monotonic()))
                        self._servo_ticks += 1
                    except Exception as e:
                        self._fault = f"servo send failed: {type(e).__name__}: {e}"
                        log.exception("YAM servo send failed")
            nxt += period
            delay = nxt - time.monotonic()
            if delay > 0:
                self._servo_stop.wait(delay)
            else:
                nxt = time.monotonic()

    def _sdk_fault(self) -> str | None:
        r = self._robot
        if r is None or self.sim:
            return None
        chain = getattr(r, "motor_chain", None)
        if chain is not None and not getattr(chain, "running", True):
            return "SDK motor chain stopped (motor error, CAN failure or measured joint-limit violation)"
        th = getattr(r, "_server_thread", None)
        if th is not None and not th.is_alive():
            return "SDK robot server thread died"
        return None

    # ------------------------------------------------------------------ state
    def read_state(self) -> ArmState:
        with self._lock:
            obs = self._require().get_observations()
            target = self._seg_to.copy()
        q = tuple(float(v) for v in np.asarray(obs["joint_pos"])[: self.dof])
        qd = tuple(float(v) for v in np.asarray(obs.get("joint_vel", np.zeros(self.dof)))[: self.dof])
        g = float(np.asarray(obs["gripper_pos"]).reshape(-1)[0]) if self.has_gripper and "gripper_pos" in obs else 0.0
        moving = any(abs(v) > 0.02 for v in qd) or bool(np.max(np.abs(target - np.array(q))) > 0.01)
        return ArmState(t=time.monotonic(), joints=q, gripper=g, ee_pose=self.fk(q), joint_velocities=qd, moving=moving)

    def get_state(self) -> dict[str, Any]:
        """``{joint_pos, joint_vel, ee_pose, gripper, ts}`` (TDM-46 brief); see ``state_dict``."""
        return state_dict(self)

    def status(self) -> ArmStatus:
        with self._lock:
            connected = self._robot is not None
            fault = self._fault or self._sdk_fault()
            elapsed = time.monotonic() - self._servo_t0 if self._servo_t0 else 0.0
            detail: dict[str, Any] = {
                "sim": self.sim, "kinematics": self.kin.backend, "commands": self.commands,
                "ik_failures": self.ik_failures, "smoothing": self.smoothing,
            }
            if connected and self.smoothing and elapsed > 0:
                detail["servo_hz_avg"] = round(self._servo_ticks / elapsed, 1)
            chain = getattr(self._robot, "motor_chain", None)
            if chain is not None and hasattr(chain, "comm_freq"):
                try:
                    detail["sdk_loop_hz"] = round(float(chain.comm_freq), 1)
                except Exception:  # noqa: BLE001
                    pass
            return ArmStatus(connected=connected, enabled=connected and fault is None, holding=self._holding,
                             estopped=self._estopped, fault=fault, detail=detail)

    # ------------------------------------------------------------------ kinematics
    def fk(self, joints: Sequence[float]) -> Pose:
        return Pose.from_matrix(self.kin.fk(joints).tolist())

    def ik(self, pose: Pose, seed: Sequence[float] | None = None) -> tuple[float, ...]:
        t = np.array(pose.matrix())
        if seed is None:
            seed = self.joint_positions() if self._robot is not None else self.home_joints
        try:
            q = self.kin.ik(t, seed, extra_seeds=[self.home_joints])
        except IKError:
            self.ik_failures += 1
            raise
        return tuple(float(v) for v in q)

    # ------------------------------------------------------------------ motion
    def command_joints(self, joints: Sequence[float], gripper: float | None = None) -> None:
        if len(joints) != self.dof:
            raise ArmError(f"expected {self.dof} joints, got {len(joints)}")
        q = np.array([float(v) for v in joints])
        if not np.all(np.isfinite(q)):
            raise ArmError(f"non-finite joint command {list(joints)}")
        with self._lock:
            self._require()
            if self._estopped:
                raise ArmStopped("soft e-stop is latched")
            if gripper is not None:
                self._gripper_cmd = min(max(float(gripper), 0.0), 1.0)
            now = time.monotonic()
            if self.smoothing:
                start = self._setpoint_now(now) if not self._holding else self._seg_to
                dt = now - self._last_cmd_t if self._last_cmd_t is not None else 1.0 / self.control_rate_hz
                self._seg_from = np.array(start, dtype=float)
                self._seg_to = q
                self._seg_t0 = now
                self._seg_dur = min(max(dt, 1.0 / self.servo_rate_hz), 2.0 / self.control_rate_hz)
            else:
                self._seg_from = q
                self._seg_to = q
                self._send(q)
            self._last_cmd_t = now
            self._holding = False
            self.last_joint_cmd = tuple(float(v) for v in np.clip(q, self.sdk_clip_limits.lower, self.sdk_clip_limits.upper))
            self.commands += 1

    def command_gripper(self, gripper: float) -> None:
        with self._lock:
            self._require()
            if self._estopped:
                raise ArmStopped("soft e-stop is latched")
            self._gripper_cmd = min(max(float(gripper), 0.0), 1.0)
            if self._holding or not self.smoothing:
                self._send(self._seg_to)

    def _hold_locked(self) -> None:
        q, _ = self._measured()
        self._seg_from = q.copy()
        self._seg_to = q.copy()
        self._seg_t0 = time.monotonic()
        self._last_cmd_t = None
        self._holding = True
        self._send(q)

    def hold(self) -> None:
        with self._lock:
            if self._robot is None:
                return
            try:
                self._hold_locked()
            except Exception as e:
                self._fault = f"hold failed: {type(e).__name__}: {e}"
                log.exception("YAM hold failed")

    def stop(self) -> None:
        # Latch first (lock-free) so a concurrent command_joints sees it as soon as it gets the lock.
        self._estopped = True
        self.last_stop_at = time.monotonic()
        with self._lock:
            self._estopped = True
            if self._robot is None:
                return
            try:
                self._hold_locked()
            except Exception as e:
                self._fault = f"stop hold failed: {type(e).__name__}: {e}"
                log.exception("YAM soft e-stop hold failed")

    def clear_stop(self) -> None:
        with self._lock:
            self._estopped = False
            if self._robot is not None:
                self._hold_locked()

    def move_to(self, pose: Pose, duration: float, **kw: Any) -> bool:
        """Straight-line Cartesian move (see ``move_ee_linear``). On a safety-wrapped arm call
        ``move_ee_linear(wrapped, ...)`` instead so every tick passes through the wrapper."""
        return move_ee_linear(self, pose, duration, **kw)


# ---------------------------------------------------------------------- adapter-agnostic helpers
# Written against the ArmAdapter interface only, so they work on the safety-wrapped arm
# (``JobContext.arm``) and on SimArm.


def state_dict(arm: ArmAdapter) -> dict[str, Any]:
    """``{joint_pos, joint_vel, ee_pose, gripper, ts}``: one proprioceptive sample as plain data."""
    s = arm.read_state()
    return {"joint_pos": list(s.joints), "joint_vel": list(s.joint_velocities) if s.joint_velocities else None,
            "ee_pose": s.ee_pose.to_dict(), "gripper": s.gripper, "ts": s.t}


def _slerp(q0: Sequence[float], q1: Sequence[float], a: float) -> tuple[float, float, float, float]:
    d = sum(x * y for x, y in zip(q0, q1))
    if d < 0:
        q1, d = [-v for v in q1], -d
    if d > 0.9995:
        r = [x + (y - x) * a for x, y in zip(q0, q1)]
    else:
        th = math.acos(min(d, 1.0))
        s0, s1 = math.sin((1 - a) * th) / math.sin(th), math.sin(a * th) / math.sin(th)
        r = [s0 * x + s1 * y for x, y in zip(q0, q1)]
    n = math.sqrt(sum(v * v for v in r)) or 1.0
    return tuple(v / n for v in r)  # type: ignore[return-value]


def move_ee_linear(
    arm: ArmAdapter,
    pose: Pose,
    duration: float,
    *,
    gripper: float | None = None,
    start: Pose | None = None,
    should_abort: Callable[[], bool] | None = None,
    on_tick: Callable[[Pose, tuple[float, ...]], None] | None = None,
) -> bool:
    """Blocking straight-line EE move: position lerp + orientation slerp with smoothstep timing,
    IK every tick at ``arm.control_rate_hz`` (seeded by the previous solution), streamed through
    ``arm.command_joints``. Returns False if aborted (arm held). Raises IKError if a waypoint is
    unreachable (arm held first)."""
    p0 = start or arm.ee_pose()
    period = 1.0 / arm.control_rate_hz
    duration = max(float(duration), period)
    seed: tuple[float, ...] = arm.joint_positions()
    t0 = time.monotonic()
    while True:
        if should_abort and should_abort():
            arm.hold()
            return False
        a = min((time.monotonic() - t0) / duration, 1.0)
        s = a * a * (3 - 2 * a)
        target = Pose(tuple(x + (y - x) * s for x, y in zip(p0.xyz, pose.xyz)), _slerp(p0.quat, pose.quat, s))  # type: ignore[arg-type]
        try:
            seed = arm.ik(target, seed)
        except IKError:
            arm.hold()
            raise
        arm.command_joints(seed, gripper)
        if on_tick is not None:
            on_tick(target, seed)
        if a >= 1.0:
            return True
        time.sleep(period)


__all__ = ["ACTION_SPACE", "YAM_READY", "YAM_REST", "YamArm", "move_ee_linear", "state_dict"]
