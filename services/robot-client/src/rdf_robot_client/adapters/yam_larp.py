"""LarpSafeYamArm: the ``yam`` adapter for the REAL arms, built to ``docs/real-arm-safety.md`` (from LARP).

``make_adapter("yam")`` builds this class. In sim (``sim=True`` / ``YAM_SIM=1``) it is plain
:class:`~rdf_robot_client.adapters.yam.YamArm` on stock i2rt 120c3c8 (MuJoCo, FK, laptops). On hardware it
adds the LARP rules on top of YamArm:

* **LARP's i2rt fork only** (``i2rt-minimal`` 1.1.0/1.1.1, the one ``vrteleop`` runs). ``_require_larp_fork``
  refuses stock 120c3c8: a ``get_robot`` that mentions ``check_motor_config`` (it can write motor flash), or a
  ``MotorChainRobot.close`` without ``release_motors``.
* ``get_yam_robot(..., zero_gravity_mode=False)``. Auto-recovery is off: the keyword is passed as False when
  the fork has it, and ``enable_auto_recovery=True`` is refused. ``check_motor_*`` is passed False when present,
  and the startup comm-fault reset is not used.
* **The same flock as LARP** (``/run/lock/vrteleop-arm-<channel>.lock``, else ``/tmp``), held from before
  the SDK opens the bus until after it is closed, so GeonosisAI and vrteleop can never share a bus.
* **Hold the measured pose on enable.** The fork does this in ``get_yam_robot``. YamArm then seeds its setpoint
  with the measured joints and sends exactly that.
* **Refuse to start** on any motor fault, any measured joint outside LARP's limits (+0.05 rad), or gains that
  are not LARP's ``kp [80,80,80,40,10,10]`` / ``kd [5,5,5,1.5,1.5,1.5]``. The arm is then released the LARP way
  and ``connect`` raises.
* **Joint-space rate limit <= 0.3 rad/s on every setpoint** sent to the SDK (``max_joint_speed``, which can
  only go lower). The servo thread always runs on hardware, so any first command, absolute target, home pose
  or first policy action is a slow straight line in joint space from the last setpoint, never a step.
  ``hold()`` / ``stop()`` re-seed it at the measured pose.
* **LARP's joint limits** (``robot_models/arm/yam/yam.xml`` in the fork). The TDM-45 SafetyLayer tightens
  inside them (``margin_rad``), so the GeonosisAI box always sits inside LARP's.
* **Shutdown the LARP way:** ``close(release_motors=True)``. The fork damps the arm (kp 0) until every joint
  is below 0.05 rad/s for 0.3 s (capped at 4.5 s), then disables, or stays in damping if still moving.
  After a settled release **the arm goes limp**: fold or support it first.
* **No gripper calibration sweep by surprise:** without ``YAM_GRIPPER_LIMITS`` the fork calibrates the linear
  gripper on connect, which is motion. That needs ``YAM_ALLOW_GRIPPER_CALIBRATION=1``, set by the human at the
  e-stop.

Never referenced here (``tests/test_tdm119_yam_larp.py`` fails the build if they appear anywhere in the
robot client): ``save_zero_position``, ``set_zero``, register writes or flash saves, ``motor_check``.
"""
from __future__ import annotations

import inspect
import logging
import os
import re
import threading
import time
from collections.abc import Sequence
from typing import Any

import numpy as np

from .base import ArmError, JointLimits
from .yam import YamArm
from .yam_kinematics import SDK_CLIP_BUFFER, YAM_V1_LIMITS, make_kinematics

log = logging.getLogger("rdf_robot_client.yam_larp")

# LARP's fork: i2rt/i2rt/robot_models/arm/yam/yam.xml, robots/config/yam.yml (docs/real-arm-safety.md)
LARP_YAM_LOWER = (-2.61799, 0.0, 0.0, -1.5708, -1.5708, -2.0944)
LARP_YAM_UPPER = (3.05433, 3.65, 3.66519, 1.5708, 1.5708, 2.0944)
LARP_YAM_LIMITS = JointLimits(lower=LARP_YAM_LOWER, upper=LARP_YAM_UPPER, max_velocity=YAM_V1_LIMITS.max_velocity)
LARP_YAM_KP = (80.0, 80.0, 80.0, 40.0, 10.0, 10.0)
LARP_YAM_KD = (5.0, 5.0, 5.0, 1.5, 1.5, 1.5)
LARP_GRIPPER_KP_KD = (20.0, 0.5)
LARP_I2RT_VERSIONS = ("1.1.0", "1.1.1")
LARP_LOCK_DIRS = ("/run/lock", "/tmp")
MAX_JOINT_SPEED = 0.3          # rad/s: the hard ceiling for every setpoint on the real arm
START_LIMIT_TOL = 0.05         # rad past LARP's limits that still counts as "inside" at startup (J2/J3 rest at 0)
DM_OK = 0x1                    # DM feedback state nibble: enabled, no fault


def larp_lock_path(channel: str, lock_dir: str | None = None) -> str:
    """LARP's ``_arm_channel_lock_path``: ``<dir>/vrteleop-arm-<channel>.lock``."""
    lock_dir = lock_dir or os.environ.get("YAM_LOCK_DIR") or next(
        (d for d in LARP_LOCK_DIRS if os.path.isdir(d) and os.access(d, os.W_OK)), "/tmp")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(channel)) or "_"
    return os.path.join(lock_dir, f"vrteleop-arm-{safe}.lock")


def larp_take_bus_lock(channel: str, lock_dir: str | None = None) -> int:
    """Exclusive non-blocking flock on the channel's lock file, the same one LARP's arm children take.
    Returns the fd (keep it open). Raises ArmError naming the holder if the bus is taken."""
    import fcntl

    path = larp_lock_path(channel, lock_dir)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        writable = True
    except PermissionError:
        fd = os.open(path, os.O_RDONLY)
        writable = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        holder = ""
        try:
            text = os.pread(fd, 32, 0).decode("ascii", errors="ignore").strip()
            holder = f" (pid {text})" if text.isdigit() else ""
        except OSError:
            pass
        os.close(fd)
        raise ArmError(f"CAN {channel} is in use by another arm process{holder} (lock {path}); "
                       "stop it first (vrteleop: sudo systemctl stop vrteleop-rh)") from None
    if writable:
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
        except OSError:
            pass
    return fd


def larp_release_bus_lock(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)  # closing drops the flock
        except OSError:
            pass


def _require_larp_fork() -> str:
    """The installed i2rt must be LARP's fork. Returns its version; raises ArmError otherwise."""
    import importlib.metadata

    try:
        from i2rt.robots import get_robot as gr
        from i2rt.robots.motor_chain_robot import MotorChainRobot
    except ImportError as e:
        raise ArmError("LARP's i2rt fork is not installed in this venv (scripts/jetson/remote/tdm118_user_setup.sh "
                       "--i2rt-larp <dir>); see docs/real-arm-safety.md") from e
    version = importlib.metadata.version("i2rt")
    problems = []
    if version not in LARP_I2RT_VERSIONS:
        problems.append(f"i2rt {version} is not LARP's fork ({'/'.join(LARP_I2RT_VERSIONS)})")
    try:
        src = inspect.getsource(gr)
    except (OSError, TypeError):
        src = ""
    if "check_motor_config" in src:
        problems.append("its get_robot runs check_motor_config (can write motor flash): this is stock i2rt")
    if "release_motors" not in inspect.signature(MotorChainRobot.close).parameters:
        problems.append("MotorChainRobot.close has no release_motors (no damp-until-settled shutdown)")
    if problems:
        raise ArmError("refusing the real arm: " + "; ".join(problems) + " (docs/real-arm-safety.md)")
    return version


class LarpSafeYamArm(YamArm):
    """YamArm with docs/real-arm-safety.md enforced on hardware. See the module docstring."""

    adapter_version = "yam-larp-i2rt-fork"

    def __init__(self, *, max_joint_speed: float | None = None, lock_dir: str | None = None,
                 servo_thread: bool = True, **kw: Any) -> None:
        if kw.get("enable_auto_recovery"):
            raise ArmError("enable_auto_recovery=True is not allowed on the real arm (docs/real-arm-safety.md)")
        super().__init__(**kw)
        self.real = not self.sim
        self.lock_dir = lock_dir
        self._lock_fd: int | None = None
        self._servo_thread_enabled = servo_thread
        speed = float(max_joint_speed or os.environ.get("YAM_MAX_JOINT_SPEED") or MAX_JOINT_SPEED)
        self.max_joint_speed = min(speed, MAX_JOINT_SPEED) if speed > 0 else MAX_JOINT_SPEED
        self._last_sent: np.ndarray | None = None
        self._last_sent_t = 0.0
        self.rate_limited = 0
        self.fork_version: str | None = None
        if self.real:
            self.adapter_version = "yam-larp-i2rt-fork"
            self.smoothing = True  # the servo thread is what walks the rate-limited setpoint to the target
            self.limits = LARP_YAM_LIMITS
            self.sdk_clip_limits = JointLimits(
                lower=tuple(v - SDK_CLIP_BUFFER for v in LARP_YAM_LOWER),
                upper=tuple(v + SDK_CLIP_BUFFER for v in LARP_YAM_UPPER),
                max_velocity=LARP_YAM_LIMITS.max_velocity)
            self.kin = make_kinematics(kw.get("kinematics") or os.environ.get("YAM_KINEMATICS", "auto"),
                                       arm_type=self.arm_type,
                                       gripper=self.gripper_type if self.has_gripper else "no_gripper",
                                       limits=self.limits)

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        if self.real:
            d.update({"sdk": f"LARP i2rt fork {self.fork_version or '(not connected)'}",
                      "max_joint_speed": self.max_joint_speed, "bus_lock": larp_lock_path(self.channel, self.lock_dir),
                      "joint_limits": {"lower": list(LARP_YAM_LOWER), "upper": list(LARP_YAM_UPPER),
                                       "source": "LARP i2rt fork yam.xml (docs/real-arm-safety.md)"}})
        return d

    # ------------------------------------------------------------------ SDK construction (hardware)
    def _make_robot(self) -> Any:
        if self._robot_factory is not None or not self.real:
            return super()._make_robot()
        self.fork_version = _require_larp_fork()
        if self.has_gripper and self.gripper_limits is None and os.environ.get("YAM_ALLOW_GRIPPER_CALIBRATION") != "1":
            raise ArmError("no YAM_GRIPPER_LIMITS: the fork would run the gripper calibration sweep on connect, "
                           "which is motion. Set YAM_GRIPPER_LIMITS=closed,open, or YAM_ALLOW_GRIPPER_CALIBRATION=1 "
                           "with a human at the e-stop")
        from i2rt.robots.get_robot import get_yam_robot
        from i2rt.robots.utils import ArmType, GripperType

        params = inspect.signature(get_yam_robot).parameters
        kw: dict[str, Any] = {
            "channel": self.channel,
            "arm_type": ArmType.from_string_name(self.arm_type),
            "gripper_type": GripperType.from_string_name(self.gripper_type),
            "zero_gravity_mode": False,
        }
        for name, value in (("enable_auto_recovery", False), ("check_motor_config", False),
                            ("check_motor_types", False), ("reset_startup_communication_fault", False)):
            if name in params:
                kw[name] = value
        if self.gripper_limits is not None and self.has_gripper:
            kw["gripper_limits_override"] = np.array(self.gripper_limits, dtype=float)
        log.info("connecting YAM (LARP fork %s): channel=%s gripper=%s", self.fork_version, self.channel,
                 self.gripper_type)
        return get_yam_robot(**kw)

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        if not self.real:
            super().connect()
            return
        with self._lock:
            if self._robot is not None:
                return
            self._lock_fd = larp_take_bus_lock(self.channel, self.lock_dir)
        try:
            if not self._servo_thread_enabled:
                self.smoothing = False  # tests drive _servo_tick() by hand
            super().connect()           # builds the robot, seeds + sends the MEASURED pose (hold on enable)
            self.smoothing = True
            self._check_startup()
        except BaseException:
            self._release_and_close()
            raise

    def _check_startup(self) -> None:
        robot = self._require()
        q, _ = self._measured()
        bad = [f"j{i + 1}={v:+.3f} (limits {lo:+.3f}..{hi:+.3f})"
               for i, (v, lo, hi) in enumerate(zip(q, LARP_YAM_LOWER, LARP_YAM_UPPER))
               if not (lo - START_LIMIT_TOL <= v <= hi + START_LIMIT_TOL) or not np.isfinite(v)]
        if bad:
            raise ArmError("refusing to start: measured joint(s) outside LARP's limits: " + ", ".join(bad)
                           + ". Do NOT re-zero: find the cause (docs/real-arm-safety.md)")
        chain = getattr(robot, "motor_chain", None)
        states = chain.read_states() if chain is not None and hasattr(chain, "read_states") else []
        faults = []
        for i, m in enumerate(states):
            ec = getattr(m, "error_code", DM_OK)
            code = int(ec, 16) if isinstance(ec, str) else int(ec)
            if code != DM_OK:
                faults.append(f"motor {i + 1}: 0x{code:X}")
        if faults:
            raise ArmError("refusing to start: motor fault(s) " + ", ".join(faults)
                           + ". After a crash expect 0xD: power-cycle the arm, never loop clear_error")
        kp, kd = getattr(robot, "_kp", None), getattr(robot, "_kd", None)
        if kp is not None and kd is not None:
            want_kp = np.array(LARP_YAM_KP + ((LARP_GRIPPER_KP_KD[0],) if self.has_gripper else ()))
            want_kd = np.array(LARP_YAM_KD + ((LARP_GRIPPER_KP_KD[1],) if self.has_gripper else ()))
            got_kp, got_kd = np.asarray(kp, dtype=float).ravel(), np.asarray(kd, dtype=float).ravel()
            if got_kp.shape != want_kp.shape or not np.allclose(got_kp, want_kp) or not np.allclose(got_kd, want_kd):
                raise ArmError(f"refusing to start: gains kp={got_kp.tolist()} kd={got_kd.tolist()} are not LARP's "
                               f"kp={want_kp.tolist()} kd={want_kd.tolist()}")
        if self._servo_thread_enabled and (self._servo_thread is None or not self._servo_thread.is_alive()):
            self._servo_stop.clear()
            self._servo_t0 = time.monotonic()
            self._servo_thread = threading.Thread(target=self._servo_loop, name="yam-servo", daemon=True)
            self._servo_thread.start()

    def _release_and_close(self) -> None:
        """LARP shutdown: damp until settled, then disable (the fork's close(release_motors=True)); unlock."""
        thread = None
        with self._lock:
            robot, self._robot = self._robot, None
            self._servo_stop.set()
            thread, self._servo_thread = self._servo_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        try:
            if robot is not None:
                close = robot.close
                try:
                    accepts = "release_motors" in inspect.signature(close).parameters
                except (TypeError, ValueError):
                    accepts = False
                if accepts:
                    close(release_motors=True)
                else:
                    log.error("YAM close() cannot release the motors (not LARP's fork): they stay enabled")
                    close()
        except Exception:
            log.exception("YAM release/close failed")
        finally:
            fd, self._lock_fd = self._lock_fd, None
            larp_release_bus_lock(fd)
            self._last_sent = None

    def disconnect(self) -> None:
        """Hold the measured pose, then release the LARP way: damp until settled, disable, close CAN.
        After a settled release the arm goes LIMP: fold or support it first."""
        if not self.real:
            super().disconnect()
            return
        with self._lock:
            if self._robot is None:
                larp_release_bus_lock(self._lock_fd)
                self._lock_fd = None
                return
            try:
                self._hold_locked()
            except Exception:
                log.exception("YAM hold before release failed")
        self._release_and_close()

    # ------------------------------------------------------------------ the rate limit
    def _send(self, q: np.ndarray) -> None:
        if not self.real:
            super()._send(q)
            return
        target = np.clip(np.asarray(q, dtype=float), self.sdk_clip_limits.lower, self.sdk_clip_limits.upper)
        now = time.monotonic()
        if self._last_sent is None:
            out = target
        else:
            dt = min(max(now - self._last_sent_t, 0.0), 2.0 / self.servo_rate_hz)
            delta = target - self._last_sent
            m = float(np.max(np.abs(delta))) if delta.size else 0.0
            step = self.max_joint_speed * dt
            if m > step:
                out = self._last_sent + delta * (step / m)
                self.rate_limited += 1
            else:
                out = target
        self._last_sent, self._last_sent_t = out, now
        super()._send(out)

    def _hold_locked(self) -> None:
        """Hold the MEASURED pose: re-seed the rate limiter there, then send it."""
        if self.real:
            q, _ = self._measured()
            self._last_sent, self._last_sent_t = None, time.monotonic()
            super()._hold_locked()
            self._last_sent = np.clip(q, self.sdk_clip_limits.lower, self.sdk_clip_limits.upper)
            return
        super()._hold_locked()

    def _servo_tick(self, now: float | None = None) -> None:
        """One servo step (the thread calls the same thing at servo_rate_hz). Tests call it directly."""
        with self._lock:
            if self._robot is not None and not self._holding and not self._estopped:
                self._send(self._setpoint_now(now if now is not None else time.monotonic()))

    def move_joints(self, target: Sequence[float], **kw: Any) -> bool:  # type: ignore[override]
        """Absolute joint move: never faster than ``max_joint_speed`` (smoothstep peaks at 1.5x the mean)."""
        if self.real and kw.get("duration") is None:
            start = np.array(self.joint_positions())
            d = float(np.max(np.abs(np.asarray(target, dtype=float) - start))) if len(target) == self.dof else 0.0
            kw["duration"] = max(1.5 * d / self.max_joint_speed, 1.0 / self.control_rate_hz)
        return super().move_joints(target, **kw)


__all__ = [
    "LARP_I2RT_VERSIONS", "LARP_YAM_KD", "LARP_YAM_KP", "LARP_YAM_LIMITS", "LARP_YAM_LOWER", "LARP_YAM_UPPER",
    "MAX_JOINT_SPEED", "LarpSafeYamArm", "larp_lock_path", "larp_release_bus_lock", "larp_take_bus_lock",
]
