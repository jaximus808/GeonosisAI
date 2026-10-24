"""SimArm (TDM-49): a kinematic YAM-like arm for laptops and CI. No physics, no hardware.

The joints track the last setpoint at up to ``limits.max_velocity`` (integrated lazily on every
call, so no background thread), the gripper at ``gripper_speed`` per second. ``stop()`` freezes
the arm where it is *now* and latches, exactly like the real soft e-stop path, and records
``last_stop_at`` (monotonic) so tests can measure e-stop latency.

Kinematics: a 6R chain with YAM-like proportions (base yaw, shoulder pitch, elbow pitch,
forearm roll, wrist pitch, wrist roll; ~0.6 m reach). It is NOT the real YAM's geometry: TDM-46
owns the real model. IK is damped least squares on a finite-difference Jacobian (pure Python).

Everything this adapter reports is simulated; ``describe()`` says ``"sim": true`` and the robot
registers with embodiment ``sim_yam`` so nothing downstream mistakes it for the arm.
"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Sequence
from typing import Any

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
    quat_to_rot,
)

Mat = list[list[float]]

# (parent -> joint translation in metres, rotation axis) for each joint, then the tool offset.
_CHAIN: tuple[tuple[tuple[float, float, float], str], ...] = (
    ((0.0, 0.0, 0.09), "z"),  # J1 base yaw
    ((0.0, 0.0, 0.04), "y"),  # J2 shoulder pitch
    ((0.26, 0.0, 0.0), "y"),  # J3 elbow pitch
    ((0.12, 0.0, 0.0), "x"),  # J4 forearm roll
    ((0.12, 0.0, 0.0), "y"),  # J5 wrist pitch
    ((0.05, 0.0, 0.0), "x"),  # J6 wrist roll
)
_TOOL = (0.08, 0.0, 0.0)

SIM_LIMITS = JointLimits(
    lower=(-2.8, -2.0, -0.2, -2.8, -1.9, -2.8),
    upper=(2.8, 1.0, 2.8, 2.8, 1.9, 2.8),
    max_velocity=(3.0, 3.0, 3.0, 4.0, 4.0, 4.0),
)
SIM_HOME = (0.0, -1.0, 1.6, 0.0, 0.9, 0.0)  # elbow up, tool pointing down, ~0.35 m out


def _rot(axis: str, a: float) -> Mat:
    c, s = math.cos(a), math.sin(a)
    if axis == "x":
        return [[1, 0, 0], [0, c, -s], [0, s, c]]
    if axis == "y":
        return [[c, 0, s], [0, 1, 0], [-s, 0, c]]
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def _mm(a: Mat, b: Mat) -> Mat:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _mv(a: Mat, v: Sequence[float]) -> list[float]:
    return [sum(a[i][k] * v[k] for k in range(3)) for i in range(3)]


def _t(a: Mat) -> Mat:
    return [[a[j][i] for j in range(3)] for i in range(3)]


def _fk(q: Sequence[float]) -> tuple[list[float], Mat]:
    p = [0.0, 0.0, 0.0]
    r: Mat = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    for (off, axis), qi in zip(_CHAIN, q):
        d = _mv(r, off)
        p = [p[0] + d[0], p[1] + d[1], p[2] + d[2]]
        r = _mm(r, _rot(axis, qi))
    d = _mv(r, _TOOL)
    return [p[0] + d[0], p[1] + d[1], p[2] + d[2]], r


def _rotvec(r: Mat) -> list[float]:
    """Axis-angle vector of a rotation matrix."""
    cos_a = max(-1.0, min(1.0, (r[0][0] + r[1][1] + r[2][2] - 1) / 2))
    a = math.acos(cos_a)
    v = [r[2][1] - r[1][2], r[0][2] - r[2][0], r[1][0] - r[0][1]]
    if a < 1e-6:
        return [x / 2 for x in v]
    s = math.sin(a)
    if s < 1e-6:  # a ~ pi: use the diagonal
        ax = [math.sqrt(max(0.0, (r[i][i] + 1) / 2)) for i in range(3)]
        return [x * a for x in ax]
    return [x * a / (2 * s) for x in v]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small dense systems)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda i: abs(m[i][c]))
        if abs(m[piv][c]) < 1e-12:
            raise IKError("singular system")
        m[c], m[piv] = m[piv], m[c]
        for i in range(c + 1, n):
            f = m[i][c] / m[c][c]
            for j in range(c, n + 1):
                m[i][j] -= f * m[c][j]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (m[i][n] - sum(m[i][j] * x[j] for j in range(i + 1, n))) / m[i][i]
    return x


class SimArm(ArmAdapter):
    name = "sim"
    embodiment = "sim_yam"
    adapter_version = "sim-tdm49"
    dof = 6
    control_rate_hz = 30.0

    def __init__(self, *, limits: JointLimits = SIM_LIMITS, home: Sequence[float] = SIM_HOME,
                 start: Sequence[float] | None = None, gripper_speed: float = 2.0) -> None:
        self.limits = limits
        self.home_joints = tuple(home)
        self._q = list(start if start is not None else home)
        self._target = self._q[:]
        self._g = 1.0
        self._g_target = 1.0
        self.gripper_speed = gripper_speed
        self._lock = threading.RLock()
        self._last = time.monotonic()
        self._connected = False
        self._holding = True
        self._estopped = False
        self.last_stop_at: float | None = None
        self.last_command_at: float | None = None
        self.commands = 0
        self.clamped = 0

    # ---- integration
    def _advance(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if dt <= 0:
            return
        for i in range(self.dof):
            step = self.limits.max_velocity[i] * dt
            err = self._target[i] - self._q[i]
            self._q[i] += max(-step, min(step, err))
        gerr = self._g_target - self._g
        gstep = self.gripper_speed * dt
        self._g += max(-gstep, min(gstep, gerr))

    def _require(self) -> None:
        if not self._connected:
            raise ArmNotConnected("sim arm is not connected")

    # ---- lifecycle
    def connect(self) -> None:
        with self._lock:
            self._advance()
            self._connected = True
            self._target = self._q[:]
            self._holding = True

    def disconnect(self) -> None:
        with self._lock:
            if self._connected:
                self._advance()
                self._target = self._q[:]
            self._connected = False

    # ---- state
    def read_state(self) -> ArmState:
        with self._lock:
            self._advance()
            q = tuple(self._q)
            moving = any(abs(t - c) > 1e-4 for t, c in zip(self._target, self._q)) or abs(self._g_target - self._g) > 1e-4
            return ArmState(t=self._last, joints=q, gripper=self._g, ee_pose=self.fk(q), moving=moving)

    def status(self) -> ArmStatus:
        with self._lock:
            return ArmStatus(connected=self._connected, enabled=self._connected, holding=self._holding,
                             estopped=self._estopped, fault=None,
                             detail={"sim": True, "commands": self.commands, "clamped": self.clamped})

    # ---- kinematics
    def fk(self, joints: Sequence[float]) -> Pose:
        p, r = _fk(joints)
        return Pose.from_matrix([r[0] + [p[0]], r[1] + [p[1]], r[2] + [p[2]]])

    def ik(self, pose: Pose, seed: Sequence[float] | None = None, *, max_iters: int = 150,
           pos_tol: float = 1e-4, rot_tol: float = 2e-3) -> tuple[float, ...]:
        r_t = quat_to_rot(pose.quat)
        seeds = [list(seed) if seed is not None else list(self.joint_positions()), list(self.home_joints)]
        best: tuple[float, list[float]] | None = None
        for q in seeds:
            q = list(self.limits.clamp(q))
            lam2 = 1e-3
            for _ in range(max_iters):
                e = self._err(q, pose.xyz, r_t)
                pe, re = math.sqrt(sum(v * v for v in e[:3])), math.sqrt(sum(v * v for v in e[3:]))
                if best is None or pe + 0.1 * re < best[0]:
                    best = (pe + 0.1 * re, q[:])
                if pe < pos_tol and re < rot_tol:
                    return tuple(q)
                jac = self._jacobian(q, pose.xyz, r_t, e)
                jjt = [[sum(jac[i][k] * jac[j][k] for k in range(self.dof)) + (lam2 if i == j else 0.0)
                        for j in range(6)] for i in range(6)]
                y = _solve(jjt, e)
                dq = [sum(jac[i][k] * y[i] for i in range(6)) for k in range(self.dof)]
                n = max(abs(v) for v in dq)
                if n > 0.3:
                    dq = [v * 0.3 / n for v in dq]
                q = list(self.limits.clamp([a + b for a, b in zip(q, dq)]))
        raise IKError(f"sim IK did not converge for {pose.to_dict()} (best residual {best[0] if best else 'n/a':.4})")

    def _err(self, q: Sequence[float], xyz: Sequence[float], r_t: Mat) -> list[float]:
        p, r = _fk(q)
        return [xyz[0] - p[0], xyz[1] - p[1], xyz[2] - p[2], *_rotvec(_mm(r_t, _t(r)))]

    def _jacobian(self, q: list[float], xyz: Sequence[float], r_t: Mat, e0: list[float]) -> list[list[float]]:
        h = 1e-5
        cols = []
        for k in range(self.dof):
            qk = q[:]
            qk[k] += h
            ek = self._err(qk, xyz, r_t)
            cols.append([(e0[i] - ek[i]) / h for i in range(6)])  # d(pose)/dq = -d(err)/dq
        return [[cols[k][i] for k in range(self.dof)] for i in range(6)]

    # ---- motion
    def command_joints(self, joints: Sequence[float], gripper: float | None = None) -> None:
        if len(joints) != self.dof:
            raise ArmError(f"expected {self.dof} joints, got {len(joints)}")
        if any(not math.isfinite(v) for v in joints):
            raise ArmError(f"non-finite joint command {list(joints)}")
        with self._lock:
            self._require()
            if self._estopped:
                raise ArmStopped("soft e-stop is latched")
            self._advance()
            q = self.limits.clamp(joints)
            if q != tuple(joints):
                self.clamped += 1
            self._target = list(q)
            self.last_joint_cmd = q
            if gripper is not None:
                self._g_target = min(max(float(gripper), 0.0), 1.0)
            self._holding = False
            self.commands += 1
            self.last_command_at = time.monotonic()

    def command_gripper(self, gripper: float) -> None:
        with self._lock:
            self._require()
            if self._estopped:
                raise ArmStopped("soft e-stop is latched")
            self._advance()
            self._g_target = min(max(float(gripper), 0.0), 1.0)

    def hold(self) -> None:
        with self._lock:
            self._advance()
            self._target = self._q[:]
            self._g_target = self._g
            self._holding = True

    def stop(self) -> None:
        with self._lock:
            self._advance()
            self._target = self._q[:]
            self._g_target = self._g
            self._holding = True
            self._estopped = True
            self.last_stop_at = time.monotonic()

    def clear_stop(self) -> None:
        with self._lock:
            self._advance()
            self._target = self._q[:]
            self._estopped = False
            self._holding = True

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "sim": True, "note": "simulated arm (TDM-49 SimArm), not hardware"}
