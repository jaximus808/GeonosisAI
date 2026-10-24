"""YAM kinematics (TDM-46): FK / IK for the I2RT YAM tool point, with or without the I2RT SDK.

Two backends, same interface (``fk(q) -> 4x4``, ``ik(T, seed) -> q``, ``limits``):

  * ``SdkKinematics``  the SDK's own kinematics: ``i2rt.robots.kinematics.Kinematics`` (MuJoCo model
                       + ``mink`` differential IK) on the SDK-combined arm+gripper MJCF, site
                       ``grasp_site``. We add ``mink.ConfigurationLimit`` so solutions respect the
                       joint ranges (the SDK example passes no limits and lets the driver clip).
                       If mink does not converge we retry with the PoE solver below and accept only
                       if the SDK's own FK confirms the result.
  * ``PoeKinematics``  pure numpy, no SDK: product of exponentials with the official screw axes and
                       home transform published in the SDK at
                       ``i2rt/robot_models/arm/yam/v1/README.md`` (i2rt-robotics/i2rt @ 120c3c8),
                       plus the ``grasp_site`` offset of the selected gripper (read from the SDK's
                       MJCF). Damped-least-squares IK with an analytic Jacobian. Used on laptops
                       without the SDK, in CI, and as a cross-check of the SDK model
                       (``tests/test_tdm46_yam.py`` checks the two agree to < 1e-6 m).

Frames: base frame of the arm (the SDK's world frame), metres. The tool point is the gripper's
``grasp_site`` (midpoint of the finger tips); its +Z axis is the approach direction. At q = 0 the
tool points along base +X.
"""
from __future__ import annotations

import math
import threading
from collections.abc import Sequence

import numpy as np

from .base import IKError, JointLimits

# --------------------------------------------------------------------------- published YAM v1 model

# Space-frame screw axes (omega, v) at q = 0, from i2rt/robot_models/arm/yam/v1/README.md.
YAM_V1_SCREWS = np.array([
    [0, 0, 1, 0.0, 0.0, 0.0],
    [0, 1, 0, -0.1135, 0.0, 0.02],
    [0, -1, 0, 0.113499, 0.0, 0.244],
    [0, -1, 0, 0.1735, 0.0, -0.000999],
    [0, 0, -1, 0.0, 0.074998, 0.0],
    [1, 0, 0, 0.0, 0.173501, 0.0],
], dtype=float)

# Gripper mount frame (``gripper`` body = joint6 child) at q = 0, base frame. README "Home configuration M".
YAM_V1_M_MOUNT = np.array([
    [0.0, 0.0, 1.0, 0.110598],
    [-1.0, 0.0, 0.0, 0.0],
    [0.0, -1.0, 0.0, 0.173501],
    [0.0, 0.0, 0.0, 1.0],
])

# ``grasp_site`` in the mount frame, per gripper (computed from the SDK's combined MJCF at q = 0;
# rotation is Rz(+90deg) for all of them except flexible_4310, Rz(-90deg)). Translation in metres.
_RZ90 = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
_TOOL_OFFSETS: dict[str, tuple[np.ndarray, tuple[float, float, float]]] = {
    "linear_4310": (_RZ90, (0.000039, 0.000097, 0.14465)),
    "linear_3507": (_RZ90, (0.0, 0.0, 0.1347)),
    "crank_4310": (_RZ90, (-0.000088, 0.04459, 0.14676)),
    "flexible_4310": (_RZ90.T, (0.0, 0.0007, 0.141752)),
    "no_gripper": (_RZ90, (0.0, 0.0, 0.0)),
}

# Joint ranges from yam.urdf / yam.xml (rad). The SDK's driver clips commands to these +/- 0.15 rad
# (``get_robot.get_yam_robot``: "safety buffer") and raises if the MEASURED pose leaves them by
# more than 0.15 + 0.1 rad. ``YamArm.limits`` exposes the unbuffered ranges.
YAM_V1_LOWER = (-2.61799, 0.0, 0.0, -1.69297, -1.5708, -2.0944)
YAM_V1_UPPER = (3.14159, 3.66519, 3.14159, 1.5708, 1.5708, 2.0944)
SDK_CLIP_BUFFER = 0.15
# URDF <limit velocity="1"> for every joint. Almost certainly a placeholder, but it is what the
# official model says; safety (TDM-45) applies its own, lower caps anyway.
YAM_V1_URDF_VELOCITY = (1.0, 1.0, 1.0, 1.0, 1.0, 1.0)

YAM_V1_LIMITS = JointLimits(lower=YAM_V1_LOWER, upper=YAM_V1_UPPER, max_velocity=YAM_V1_URDF_VELOCITY)

YAM_JOINT_NAMES = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6"]  # SDK / URDF names


def tool_offset(gripper: str) -> np.ndarray:
    try:
        rot, xyz = _TOOL_OFFSETS[gripper]
    except KeyError:
        raise ValueError(f"no grasp_site offset for gripper {gripper!r}; known: {sorted(_TOOL_OFFSETS)}") from None
    t = np.eye(4)
    t[:3, :3] = rot
    t[:3, 3] = xyz
    return t


# --------------------------------------------------------------------------- small SE(3) helpers


def _skew(w: np.ndarray) -> np.ndarray:
    return np.array([[0.0, -w[2], w[1]], [w[2], 0.0, -w[0]], [-w[1], w[0], 0.0]])


def _exp_twist(s: np.ndarray, theta: float) -> np.ndarray:
    """exp([S] theta) for a unit-rotation screw S = (w, v)."""
    w, v = s[:3], s[3:]
    wx = _skew(w)
    st, ct = math.sin(theta), math.cos(theta)
    r = np.eye(3) + st * wx + (1 - ct) * (wx @ wx)
    g = np.eye(3) * theta + (1 - ct) * wx + (theta - st) * (wx @ wx)
    t = np.eye(4)
    t[:3, :3] = r
    t[:3, 3] = g @ v
    return t


def rotvec(r: np.ndarray) -> np.ndarray:
    """Axis-angle vector of a rotation matrix (robust near 0 and pi)."""
    cos_a = float(np.clip((np.trace(r) - 1.0) / 2.0, -1.0, 1.0))
    a = math.acos(cos_a)
    v = np.array([r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]])
    if a < 1e-7:
        return v / 2.0
    if math.pi - a < 1e-4:
        # near pi: axis from the symmetric part
        b = (r + np.eye(3)) / 2.0
        i = int(np.argmax(np.diag(b)))
        axis = b[:, i] / math.sqrt(max(b[i, i], 1e-12))
        if np.dot(axis, v) < 0:
            axis = -axis
        return axis * a
    return v * (a / (2.0 * math.sin(a)))


def pose_error(t_target: np.ndarray, t_current: np.ndarray) -> tuple[float, float]:
    """(position error m, rotation error rad)."""
    dp = float(np.linalg.norm(t_target[:3, 3] - t_current[:3, 3]))
    dr = float(np.linalg.norm(rotvec(t_target[:3, :3] @ t_current[:3, :3].T)))
    return dp, dr


# --------------------------------------------------------------------------- PoE backend


class PoeKinematics:
    """Pure-numpy YAM v1 kinematics (official screw axes) for any shipped gripper's grasp_site."""

    backend = "poe"

    def __init__(self, gripper: str = "linear_4310", limits: JointLimits = YAM_V1_LIMITS,
                 tool: np.ndarray | None = None) -> None:
        self.gripper = gripper
        self.limits = limits
        self._screws = YAM_V1_SCREWS
        self._m = YAM_V1_M_MOUNT @ (tool if tool is not None else tool_offset(gripper))
        self._lo = np.array(limits.lower)
        self._hi = np.array(limits.upper)

    def fk(self, q: Sequence[float]) -> np.ndarray:
        t = np.eye(4)
        for s, th in zip(self._screws, q):
            t = t @ _exp_twist(s, float(th))
        return t @ self._m

    def _fk_and_jac(self, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Tool pose and the 6x6 Jacobian d[p_tool; omega]/dq in the base frame."""
        t = np.eye(4)
        jac = np.zeros((6, 6))
        cols = []
        for s, th in zip(self._screws, q):
            # space-frame screw of this joint in the current configuration: Ad(T_prev) s
            r, p = t[:3, :3], t[:3, 3]
            w = r @ s[:3]
            v = r @ s[3:] + np.cross(p, w)
            cols.append((w, v))
            t = t @ _exp_twist(s, float(th))
        t = t @ self._m
        p_tool = t[:3, 3]
        for i, (w, v) in enumerate(cols):
            jac[:3, i] = v + np.cross(w, p_tool)  # linear velocity of the tool point
            jac[3:, i] = w
        return t, jac

    def ik(self, t_target: np.ndarray, seed: Sequence[float] | None = None, *, pos_tol: float = 1e-4,
           rot_tol: float = 1e-3, max_iters: int = 200, extra_seeds: Sequence[Sequence[float]] = ()) -> np.ndarray:
        seeds = [np.asarray(seed, dtype=float)] if seed is not None else []
        seeds += [np.asarray(s, dtype=float) for s in extra_seeds]
        if not seeds:
            seeds = [(self._lo + self._hi) / 2.0]
        best: tuple[float, np.ndarray] | None = None
        wgt = np.array([1, 1, 1, 0.1, 0.1, 0.1])  # rotation weighted so 1 rad ~ 0.1 m

        def residual(q: np.ndarray) -> tuple[np.ndarray, np.ndarray, float, float]:
            t, jac = self._fk_and_jac(q)
            e = np.concatenate([t_target[:3, 3] - t[:3, 3], rotvec(t_target[:3, :3] @ t[:3, :3].T)])
            return e, jac, float(np.linalg.norm(e[:3])), float(np.linalg.norm(e[3:]))

        for q0 in seeds:
            q = np.clip(q0.copy(), self._lo, self._hi)
            lam2 = 1e-4
            e, jac, pe, re = residual(q)
            for _ in range(max_iters):
                score = pe + 0.05 * re
                if best is None or score < best[0]:
                    best = (score, q.copy())
                if pe < pos_tol and re < rot_tol:
                    return q
                # Levenberg-Marquardt style damped least squares: grow the damping when a step
                # makes things worse (near singularities / joint limits), shrink it when it helps.
                jw, ew = jac * wgt[:, None], e * wgt
                dq = jw.T @ np.linalg.solve(jw @ jw.T + lam2 * np.eye(6), ew)
                n = float(np.max(np.abs(dq)))
                if n > 0.3:
                    dq *= 0.3 / n
                q_new = np.clip(q + dq, self._lo, self._hi)
                e_new, jac_new, pe_new, re_new = residual(q_new)
                if np.linalg.norm(e_new * wgt) < np.linalg.norm(ew):
                    q, e, jac, pe, re = q_new, e_new, jac_new, pe_new, re_new
                    lam2 = max(lam2 / 3.0, 1e-7)
                else:
                    lam2 = min(lam2 * 10.0, 1e2)
                    if lam2 >= 1e2:
                        break  # stuck: next seed
        raise IKError(f"YAM IK (poe) did not converge (best residual {best[0] if best else float('nan'):.2e})")


# --------------------------------------------------------------------------- SDK backend


class SdkKinematics:
    """The SDK's MuJoCo/mink kinematics on the combined arm+gripper model (site ``grasp_site``)."""

    backend = "sdk"

    def __init__(self, arm_type: str = "yam", gripper: str = "linear_4310", limits: JointLimits = YAM_V1_LIMITS,
                 site: str = "grasp_site") -> None:
        import mink
        import mujoco
        from i2rt.robots.kinematics import Kinematics
        from i2rt.robots.utils import ArmType, GripperType, combine_arm_and_gripper_xml

        self.gripper = gripper
        self.limits = limits
        self.site = site
        self.xml_path = combine_arm_and_gripper_xml(ArmType.from_string_name(arm_type), GripperType.from_string_name(gripper))
        self._kin = Kinematics(self.xml_path, site)
        self._model = mujoco.MjModel.from_xml_path(self.xml_path)
        self._nq = int(self._model.nq)  # 6 arm joints (+ 2 finger slides with a gripper)
        self._ik_limits = [mink.ConfigurationLimit(self._kin._configuration.model)]
        self._lock = threading.Lock()  # mink.Configuration is stateful
        self._poe = PoeKinematics(gripper, limits) if arm_type == "yam" and gripper in _TOOL_OFFSETS else None

    def _full(self, q: Sequence[float]) -> np.ndarray:
        full = np.zeros(self._nq)
        full[:6] = np.asarray(q, dtype=float)[:6]
        return full

    def fk(self, q: Sequence[float]) -> np.ndarray:
        with self._lock:
            return np.array(self._kin.fk(self._full(q), self.site))

    def ik(self, t_target: np.ndarray, seed: Sequence[float] | None = None, *, pos_tol: float = 1e-4,
           rot_tol: float = 1e-3, max_iters: int = 200, extra_seeds: Sequence[Sequence[float]] = ()) -> np.ndarray:
        seeds = ([seed] if seed is not None else []) + list(extra_seeds)
        if not seeds:
            seeds = [np.zeros(6)]
        lo, hi = np.array(self.limits.lower), np.array(self.limits.upper)
        for s in seeds:
            with self._lock:
                ok, q = self._kin.ik(t_target, self.site, init_q=self._full(np.clip(s, lo, hi)),
                                     limits=self._ik_limits, pos_threshold=pos_tol, ori_threshold=rot_tol,
                                     max_iters=max_iters)
            if ok:
                return np.array(q[:6])
        if self._poe is not None:  # mink stalled (large step / near a limit): try the PoE solver
            q = self._poe.ik(t_target, seed, pos_tol=pos_tol, rot_tol=rot_tol, extra_seeds=extra_seeds)
            pe, re = pose_error(t_target, self.fk(q))
            if pe < 5 * pos_tol and re < 5 * rot_tol:
                return q
        raise IKError("YAM IK (sdk/mink) did not converge")


def make_kinematics(backend: str = "auto", *, arm_type: str = "yam", gripper: str = "linear_4310",
                    limits: JointLimits = YAM_V1_LIMITS) -> PoeKinematics | SdkKinematics:
    """``auto`` = the SDK's kinematics when the SDK (mujoco + mink) imports, else PoE."""
    if backend in ("auto", "sdk"):
        try:
            return SdkKinematics(arm_type, gripper, limits)
        except ImportError:
            if backend == "sdk":
                raise
    if arm_type != "yam":
        raise ValueError(f"PoE kinematics only ships the YAM v1 model, not {arm_type!r}; install the I2RT SDK")
    return PoeKinematics(gripper, limits)


__all__ = [
    "SDK_CLIP_BUFFER",
    "YAM_JOINT_NAMES",
    "YAM_V1_LIMITS",
    "YAM_V1_LOWER",
    "YAM_V1_M_MOUNT",
    "YAM_V1_SCREWS",
    "YAM_V1_UPPER",
    "PoeKinematics",
    "SdkKinematics",
    "make_kinematics",
    "pose_error",
    "rotvec",
    "tool_offset",
]
