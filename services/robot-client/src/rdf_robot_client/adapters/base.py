"""The arm adapter interface (TDM-49). Every robot-client ticket drives the arm through this.

Units and conventions (all adapters):
  * joints:  radians, ``dof`` values (6 for the YAM), in the arm's own joint order.
  * gripper: one float, 0.0 = fully closed .. 1.0 = fully open. Adapters map it to hardware units.
  * Pose:    end-effector (tool point) in the robot BASE frame. ``xyz`` in metres,
             ``quat`` = (w, x, y, z) unit quaternion. ``Pose.from_rpy`` / ``Pose.rpy`` for convenience.
  * time:    ``time.monotonic()`` seconds.

Who implements / wraps what:
  * ``adapters/sim.py``      SimArm: kinematic simulation for laptops and CI (this ticket).
  * ``adapters/yam.py``      TDM-46: I2RT YAM via the I2RT SDK, with real IK. Implement the
                             abstract methods below; register it in ``adapters/__init__.py``.
  * safety layer             TDM-45: subclass ``ArmAdapterWrapper`` and override
                             ``command_joints`` / ``command_gripper`` (clamp, speed caps,
                             workspace check via ``fk``) plus a watchdog. Because the default
                             ``command_ee`` / ``move_joints`` / ``move_ee`` are written against
                             ``self.ik`` + ``self.command_joints``, EVERY motion (joint, EE, homing,
                             policy chunks, teleop deltas) funnels through the wrapper's
                             ``command_joints``. ``main.build_arm`` wraps the adapter if
                             ``rdf_robot_client.safety.SafetyLayer`` exists.
  * rollout / eval / teleop  TDM-50/51/52: never talk to the SDK; use ``JobContext.arm`` (the
                             wrapped adapter) and ``JobContext.run_control_loop`` (handlers.py).

Motion model: ``command_joints`` / ``command_ee`` / ``command_gripper`` are NON-BLOCKING setpoints
meant to be streamed at ``control_rate_hz`` (policy action chunks, teleop). ``move_joints`` /
``move_ee`` are BLOCKING, interpolated, abortable point-to-point moves built on top of them (home).

Stop semantics:
  * ``hold()``  stop following setpoints and hold the current position (servos stay on). Used for
                pause_for_takeover, waiting_reset, job end. The next command_* resumes motion.
  * ``stop()``  soft e-stop: halt NOW and LATCH. Must be safe to call from any thread at any time
                and return fast (< 50 ms). Every later command_* raises ``ArmStopped`` until
                ``clear_stop()`` (a human at the table clears it; see main.py).

Thread safety: the command loop calls ``stop()`` from the subscription thread while a job thread
streams commands, so implementations must lock their internal state.
"""
from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- errors


class ArmError(RuntimeError):
    """Any adapter failure (SDK error, bad command, not connected)."""


class ArmNotConnected(ArmError):
    pass


class ArmStopped(ArmError):
    """A motion command arrived while the soft e-stop is latched."""


class IKError(ArmError):
    """No IK solution (unreachable pose, or the solver did not converge)."""


# --------------------------------------------------------------------------- value types


@dataclass(frozen=True)
class Pose:
    """End-effector pose in the base frame: xyz metres, quat (w, x, y, z)."""

    xyz: tuple[float, float, float]
    quat: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)

    @classmethod
    def from_rpy(cls, xyz: Sequence[float], rpy: Sequence[float]) -> "Pose":
        r, p, y = rpy
        cr, sr, cp, sp, cy, sy = math.cos(r / 2), math.sin(r / 2), math.cos(p / 2), math.sin(p / 2), math.cos(y / 2), math.sin(y / 2)
        q = (cr * cp * cy + sr * sp * sy, sr * cp * cy - cr * sp * sy, cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy)
        return cls(tuple(float(v) for v in xyz), q)  # type: ignore[arg-type]

    @classmethod
    def from_matrix(cls, m: Sequence[Sequence[float]]) -> "Pose":
        """From a 4x4 (or 3x4) homogeneous transform."""
        return cls((m[0][3], m[1][3], m[2][3]), rot_to_quat([row[:3] for row in m[:3]]))

    def matrix(self) -> list[list[float]]:
        r = quat_to_rot(self.quat)
        return [r[0] + [self.xyz[0]], r[1] + [self.xyz[1]], r[2] + [self.xyz[2]], [0.0, 0.0, 0.0, 1.0]]

    @property
    def rpy(self) -> tuple[float, float, float]:
        w, x, y, z = self.quat
        roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
        pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
        yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        return (roll, pitch, yaw)

    def translated(self, dxyz: Sequence[float]) -> "Pose":
        return Pose(tuple(a + b for a, b in zip(self.xyz, dxyz)), self.quat)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, list[float]]:
        return {"xyz": [round(v, 5) for v in self.xyz], "quat": [round(v, 5) for v in self.quat]}


@dataclass(frozen=True)
class JointLimits:
    lower: tuple[float, ...]
    upper: tuple[float, ...]
    max_velocity: tuple[float, ...]  # rad/s, the arm's own cap (safety may cap lower)

    def clamp(self, q: Sequence[float]) -> tuple[float, ...]:
        return tuple(min(max(v, lo), hi) for v, lo, hi in zip(q, self.lower, self.upper))

    def within(self, q: Sequence[float], tol: float = 1e-9) -> bool:
        return all(lo - tol <= v <= hi + tol for v, lo, hi in zip(q, self.lower, self.upper))


@dataclass(frozen=True)
class ArmState:
    """One proprioceptive sample. This is what the recorder (TDM-48) logs as observation.state."""

    t: float  # time.monotonic()
    joints: tuple[float, ...]
    gripper: float
    ee_pose: Pose
    joint_velocities: tuple[float, ...] | None = None
    moving: bool = False

    def state_vector(self) -> list[float]:
        """``observation.state`` for openpi/LeRobot: joints + gripper (7 floats for the YAM)."""
        return [*self.joints, self.gripper]


@dataclass(frozen=True)
class ArmStatus:
    connected: bool
    enabled: bool = False  # servos on / torque enabled
    holding: bool = False  # hold() active and no newer setpoint
    estopped: bool = False  # soft e-stop latched
    fault: str | None = None  # hardware/SDK fault text
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.connected and self.enabled and not self.estopped and self.fault is None


@dataclass(frozen=True)
class JointTarget:
    """One streamed setpoint: joints (rad) and optionally the gripper (0..1)."""

    joints: tuple[float, ...]
    gripper: float | None = None


@dataclass(frozen=True)
class EETarget:
    """One streamed end-effector setpoint (teleop clutch deltas, Cartesian policies)."""

    pose: Pose
    gripper: float | None = None


Target = JointTarget | EETarget

CONTROL_SOURCES = ("policy", "human", "idle")  # == rdf_common.episode_format.CONTROL_SOURCES


@dataclass(frozen=True)
class ControlTick:
    """One control tick in the TDM-25 episode format (``rdf_common.episode_format``).

    Built by ``RobotClient.control_tick()`` (pull, any time, e.g. on every camera frame) and pushed
    to ``RobotClient.add_tick_listener`` callbacks by ``JobContext.run_control_loop`` after every
    streamed command. ``to_row()`` gives the actions.parquet columns (frame_index is the
    recorder's).
    """

    timestamp_us: int  # wall clock (time.time()), same clock as takeover_ts / handback_ts
    joint_pos: tuple[float, ...]  # measured
    gripper_pos: float  # measured, 0..1
    ik_target_pos: tuple[float, float, float]  # commanded EE target (FK of the joint command for joint targets)
    ik_target_quat: tuple[float, float, float, float]  # wxyz
    gripper_cmd: float  # commanded, 0..1
    control_source: str  # policy | human | idle
    joint_cmd: tuple[float, ...] | None = None  # sent to the arm after the safety layer
    t_ns: int = 0  # time.monotonic_ns(): same clock as cameras.Frame.t_ns, for frame alignment

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "timestamp_us": self.timestamp_us, "joint_pos": list(self.joint_pos), "gripper_pos": self.gripper_pos,
            "ik_target_pos": list(self.ik_target_pos), "ik_target_quat": list(self.ik_target_quat),
            "gripper_cmd": self.gripper_cmd, "control_source": self.control_source,
        }
        if self.joint_cmd is not None:
            row["joint_cmd"] = list(self.joint_cmd)
        return row

# --------------------------------------------------------------------------- the interface


class ArmAdapter(ABC):
    """Abstract arm. Subclasses implement the ``@abstractmethod``s; the rest is shared."""

    name: str = "abstract"
    embodiment: str = "i2rt_yam"  # robot.embodiment
    adapter_version: str = "0"  # robot.adapter_version
    dof: int = 6
    control_rate_hz: float = 30.0  # streaming rate the arm is happy with (policy runs at 30 fps)
    limits: JointLimits
    home_joints: tuple[float, ...]
    # Joint setpoint the HARDWARE last received (after any clamping/safety), for the recorder's
    # ``joint_cmd`` column. Adapters set it in command_joints; wrappers read it from ``inner``.
    last_joint_cmd: tuple[float, ...] | None = None

    @property
    def joint_names(self) -> list[str]:
        """For episode metadata (``make_metadata(joint_names=...)``). Override with real names."""
        return [f"j{i + 1}" for i in range(self.dof)]

    # ---- lifecycle
    @abstractmethod
    def connect(self) -> None:
        """Open the SDK/bus, enable servos, start holding the current position. Idempotent."""

    @abstractmethod
    def disconnect(self) -> None:
        """Hold, then release the hardware. Idempotent; never raises on an already-closed arm."""

    # ---- state
    @abstractmethod
    def read_state(self) -> ArmState:
        """Latest joints/gripper/EE pose. Cheap (called every control tick and by the recorder)."""

    @abstractmethod
    def status(self) -> ArmStatus:
        ...

    # ---- kinematics
    @abstractmethod
    def fk(self, joints: Sequence[float]) -> Pose:
        """Forward kinematics of the tool point."""

    @abstractmethod
    def ik(self, pose: Pose, seed: Sequence[float] | None = None) -> tuple[float, ...]:
        """Joint solution for ``pose`` near ``seed`` (default: current joints). Raises IKError."""

    # ---- motion (non-blocking setpoints)
    @abstractmethod
    def command_joints(self, joints: Sequence[float], gripper: float | None = None) -> None:
        """Stream one joint setpoint. Raises ArmStopped while the e-stop is latched."""

    @abstractmethod
    def command_gripper(self, gripper: float) -> None:
        ...

    @abstractmethod
    def hold(self) -> None:
        """Freeze at the current position (servos on). Never raises while connected."""

    @abstractmethod
    def stop(self) -> None:
        """Soft e-stop: halt immediately and latch. Thread-safe, fast, never raises."""

    @abstractmethod
    def clear_stop(self) -> None:
        """Unlatch the soft e-stop (only after a human checked the arm). Arm stays holding."""

    # ---- shared helpers (do not override in hardware adapters; wrappers inherit them as-is)
    def joint_positions(self) -> tuple[float, ...]:
        return self.read_state().joints

    def ee_pose(self) -> Pose:
        return self.read_state().ee_pose

    def command_ee(self, pose: Pose, gripper: float | None = None, seed: Sequence[float] | None = None) -> tuple[float, ...]:
        """IK then ``command_joints``. Returns the joints sent."""
        q = self.ik(pose, seed if seed is not None else self.joint_positions())
        self.command_joints(q, gripper)
        return q

    def command(self, target: Target) -> None:
        if isinstance(target, EETarget):
            self.command_ee(target.pose, target.gripper)
        else:
            self.command_joints(target.joints, target.gripper)

    def move_joints(
        self,
        target: Sequence[float],
        *,
        gripper: float | None = None,
        speed_fraction: float = 0.25,
        duration: float | None = None,
        should_abort: Callable[[], bool] | None = None,
        tolerance: float = 0.01,
        settle_timeout: float = 2.0,
    ) -> bool:
        """Blocking, linearly interpolated joint move streamed at ``control_rate_hz``.

        ``duration`` defaults to the slowest joint at ``speed_fraction`` of its max velocity.
        Returns True on arrival (within ``tolerance`` rad), False if ``should_abort()`` became true
        (the arm is then held where it is). Raises ArmStopped if the e-stop latches mid-move.
        """
        start = self.joint_positions()
        goal = tuple(float(v) for v in target)
        if len(goal) != self.dof:
            raise ArmError(f"move_joints: expected {self.dof} joints, got {len(goal)}")
        if duration is None:
            frac = max(min(speed_fraction, 1.0), 0.01)
            duration = max(
                (abs(g - s) / (vmax * frac) for g, s, vmax in zip(goal, start, self.limits.max_velocity)),
                default=0.0,
            )
        duration = max(duration, 1.0 / self.control_rate_hz)
        g0 = self.read_state().gripper
        period = 1.0 / self.control_rate_hz
        t0 = time.monotonic()
        while True:
            if should_abort and should_abort():
                self.hold()
                return False
            a = min((time.monotonic() - t0) / duration, 1.0)
            a_s = a * a * (3 - 2 * a)  # smoothstep: zero velocity at both ends
            q = tuple(s + (g - s) * a_s for s, g in zip(start, goal))
            gr = None if gripper is None else g0 + (gripper - g0) * a_s
            self.command_joints(q, gr)
            if a >= 1.0:
                break
            time.sleep(period)
        deadline = time.monotonic() + settle_timeout
        while time.monotonic() < deadline:
            if should_abort and should_abort():
                self.hold()
                return False
            if max(abs(c - g) for c, g in zip(self.joint_positions(), goal)) <= tolerance:
                return True
            time.sleep(period)
        return max(abs(c - g) for c, g in zip(self.joint_positions(), goal)) <= tolerance

    def move_ee(self, pose: Pose, **kw: Any) -> bool:
        """Blocking EE move: IK once at the goal, then ``move_joints`` (joint-space path)."""
        return self.move_joints(self.ik(pose, self.joint_positions()), **kw)

    def home(self, **kw: Any) -> bool:
        return self.move_joints(self.home_joints, **kw)

    def describe(self) -> dict[str, Any]:
        """For robot.camera_config_json / logs: static facts about this arm."""
        return {"adapter": self.name, "embodiment": self.embodiment, "adapter_version": self.adapter_version,
                "dof": self.dof, "control_rate_hz": self.control_rate_hz}

    def __enter__(self) -> "ArmAdapter":
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()


class ArmAdapterWrapper(ArmAdapter):
    """Forwards the abstract methods to ``inner``. Base class for the safety layer (TDM-45).

    The shared helpers (command_ee, move_joints, move_ee, home) are NOT forwarded: they run on the
    wrapper, so they call the wrapper's ``command_joints``. Override ``command_joints`` /
    ``command_gripper`` to filter every motion.
    """

    def __init__(self, inner: ArmAdapter) -> None:
        self.inner = inner

    # static facts come from the wrapped arm
    @property
    def name(self) -> str:  # type: ignore[override]
        return self.inner.name

    @property
    def embodiment(self) -> str:  # type: ignore[override]
        return self.inner.embodiment

    @property
    def adapter_version(self) -> str:  # type: ignore[override]
        return self.inner.adapter_version

    @property
    def dof(self) -> int:  # type: ignore[override]
        return self.inner.dof

    @property
    def control_rate_hz(self) -> float:  # type: ignore[override]
        return self.inner.control_rate_hz

    @property
    def limits(self) -> JointLimits:  # type: ignore[override]
        return self.inner.limits

    @property
    def home_joints(self) -> tuple[float, ...]:  # type: ignore[override]
        return self.inner.home_joints

    @property
    def last_joint_cmd(self) -> tuple[float, ...] | None:  # type: ignore[override]
        return self.inner.last_joint_cmd

    @property
    def joint_names(self) -> list[str]:
        return self.inner.joint_names

    def connect(self) -> None:
        self.inner.connect()

    def disconnect(self) -> None:
        self.inner.disconnect()

    def read_state(self) -> ArmState:
        return self.inner.read_state()

    def status(self) -> ArmStatus:
        return self.inner.status()

    def fk(self, joints: Sequence[float]) -> Pose:
        return self.inner.fk(joints)

    def ik(self, pose: Pose, seed: Sequence[float] | None = None) -> tuple[float, ...]:
        return self.inner.ik(pose, seed)

    def command_joints(self, joints: Sequence[float], gripper: float | None = None) -> None:
        self.inner.command_joints(joints, gripper)

    def command_gripper(self, gripper: float) -> None:
        self.inner.command_gripper(gripper)

    def hold(self) -> None:
        self.inner.hold()

    def stop(self) -> None:
        self.inner.stop()

    def clear_stop(self) -> None:
        self.inner.clear_stop()

    def describe(self) -> dict[str, Any]:
        return {**self.inner.describe(), "wrapped_by": type(self).__name__}


# --------------------------------------------------------------------------- small rotation math (no numpy)


def quat_to_rot(q: Sequence[float]) -> list[list[float]]:
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z) or 1.0
    w, x, y, z = w / n, x / n, y / n, z / n
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def rot_to_quat(r: Sequence[Sequence[float]]) -> tuple[float, float, float, float]:
    tr = r[0][0] + r[1][1] + r[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = (0.25 * s, (r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s)
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2]) * 2
        q = ((r[2][1] - r[1][2]) / s, 0.25 * s, (r[0][1] + r[1][0]) / s, (r[0][2] + r[2][0]) / s)
    elif r[1][1] > r[2][2]:
        s = math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2]) * 2
        q = ((r[0][2] - r[2][0]) / s, (r[0][1] + r[1][0]) / s, 0.25 * s, (r[1][2] + r[2][1]) / s)
    else:
        s = math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1]) * 2
        q = ((r[1][0] - r[0][1]) / s, (r[0][2] + r[2][0]) / s, (r[1][2] + r[2][1]) / s, 0.25 * s)
    if q[0] < 0:  # canonical hemisphere
        q = tuple(-v for v in q)  # type: ignore[assignment]
    return q  # type: ignore[return-value]


def pose_error(target: Pose, current: Pose) -> tuple[float, float]:
    """(position error m, rotation error rad) between two poses."""
    dp = math.dist(target.xyz, current.xyz)
    dot = abs(sum(a * b for a, b in zip(target.quat, current.quat)))
    return dp, 2 * math.acos(min(1.0, dot))


__all__ = [
    "CONTROL_SOURCES", "ControlTick", "ArmAdapter", "ArmAdapterWrapper", "ArmError", "ArmNotConnected", "ArmState", "ArmStatus", "ArmStopped",
    "EETarget", "IKError", "JointLimits", "JointTarget", "Pose", "Target", "pose_error", "quat_to_rot",
    "rot_to_quat",
]
