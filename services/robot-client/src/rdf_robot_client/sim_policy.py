"""[SIM] Scripted IK pick-and-place policy for the MuJoCo YAM demo. NOT pi0.5, not learned.

A stand-in for the policy server when there is no GPU: it plans "pick up the cube and place it in
the empty plate" with inverse kinematics and returns the same 50-step ``joint_pos_abs`` action
chunks a fine-tuned YAM checkpoint returns (TDM-41 wire format). ``sim_policy_server.py`` serves it.

How it decides (stateless per request, like a learned policy: the arm state comes in the request):
  * the robot state (6 joints + gripper) from the request, and PRIVILEGED sim state from the sim
    robot's ``/sim/state``: the scene spec's task object (``pick``), where it goes (``place``),
    finger contacts and task status (it does not look at the images; a real policy would);
  * phases: approach above the cube -> descend -> close -> lift -> carry above the empty plate ->
    lower -> release -> retreat. A grasp that closes on nothing re-opens and tries again;
  * each phase is a straight Cartesian line at ``speed_mps`` with the tool tilted 20 deg from
    vertical (a straight-down tool puts the YAM's joint4 on its limit), the fingers aligned with
    the nearest cube faces; IK is TDM-46's PoE model of the YAM.

Why it fails sometimes: an injected per-episode PERCEPTION ERROR on where it thinks the cube is
(and where the plate is), drawn once per scene reset (``scene_seq``) and scaled by the active
checkpoint's profile. The same standard-normal draw is used for every profile, so baseline and
"fine-tuned" see the same scenes and differ only in the error size:

  * ``baseline``  (the base checkpoint, ``gs://openpi-assets/checkpoints/pi05_base`` / ``base``):
    sigma 2.2 cm, yaw 0.3 rad  ->  misses a meaningful share of grasps (calibrated: see tests);
  * ``finetuned`` (any other checkpoint URI, e.g. the stub trainer's): sigma 0.5 cm, yaw 0.08 rad.

Every reply says ``"sim": true`` and names the profile. The outcome of every trial is still
real MuJoCo physics: the noise only moves where the scripted policy reaches.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .adapters.yam_kinematics import YAM_V1_LIMITS, PoeKinematics
from .policy_client import BASE_ALIASES

POLICY_NAME = "scripted_ik_pick_place"
POLICY_LABEL = "[SIM] scripted IK pick-and-place policy (MuJoCo demo stand-in, NOT pi0.5)"
HOME = (0.0, 0.98, 0.93, -0.73, 0.0, 0.0)


@dataclass(frozen=True)
class NoiseProfile:
    name: str
    pos_sigma_m: float  # cube xy perception error (per episode)
    yaw_sigma_rad: float
    place_sigma_m: float  # plate xy error

    def describe(self) -> dict[str, Any]:
        return {"profile": self.name, "pos_sigma_cm": round(self.pos_sigma_m * 100, 2),
                "yaw_sigma_rad": self.yaw_sigma_rad, "place_sigma_cm": round(self.place_sigma_m * 100, 2)}


PROFILES = {
    "baseline": NoiseProfile("baseline", 0.022, 0.30, 0.012),
    "finetuned": NoiseProfile("finetuned", 0.010, 0.12, 0.006),
}


def profile_for(checkpoint_uri: str | None) -> NoiseProfile:
    uri = (checkpoint_uri or "base").strip()
    return PROFILES["baseline"] if uri in BASE_ALIASES or uri.rstrip("/").endswith("pi05_base") else PROFILES["finetuned"]


def _unit(v: np.ndarray) -> np.ndarray:
    return v / max(float(np.linalg.norm(v)), 1e-9)


def tool_pose(x: float, y: float, z: float, phi: float, tilt: float = 0.35) -> np.ndarray:
    """Tool (grasp_site) pose: finger closing axis (tool y) horizontal along heading ``phi``; the
    approach axis tilted ``tilt`` from straight down ABOUT the finger axis (so both pads stay at the
    same height), leaning away from the base (a straight-down tool puts the YAM's joint4 on its limit)."""
    ya = np.array([math.cos(phi), math.sin(phi), 0.0])
    lean = np.array([math.sin(phi), -math.cos(phi), 0.0])  # horizontal, perpendicular to the fingers
    if float(np.dot(lean, (x, y, 0.0))) < 0:
        lean = -lean
    zt = _unit(np.array([0.0, 0.0, -math.cos(tilt)]) + math.sin(tilt) * lean)
    t = np.eye(4)
    t[:3, 0] = np.cross(ya, zt)
    t[:3, 1] = ya
    t[:3, 2] = zt
    t[:3, 3] = (x, y, z)
    return t


def grasp_heading(x: float, y: float, cube_yaw: float) -> float:
    """Closing-axis heading aligned with a cube face, nearest the arm's natural wrist roll."""
    natural = math.atan2(y, x) + math.pi / 2
    k = round((natural - cube_yaw) / (math.pi / 2))
    return cube_yaw + k * math.pi / 2


def _slerp_rot(r0: np.ndarray, r1: np.ndarray, a: float) -> np.ndarray:
    from .adapters.yam_kinematics import rotvec

    w = rotvec(r1 @ r0.T) * a
    th = float(np.linalg.norm(w))
    if th < 1e-9:
        return r0.copy()
    k = w / th
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return (np.eye(3) + math.sin(th) * kx + (1 - math.cos(th)) * kx @ kx) @ r0


class ScriptedPickPlace:
    def __init__(self, *, horizon: int = 50, dt: float = 1 / 30, speed_mps: float = 0.16, rot_speed: float = 1.4,
                 seed: int = 7) -> None:
        self.kin = PoeKinematics("linear_4310", YAM_V1_LIMITS)
        self.horizon, self.dt, self.speed, self.rot_speed, self.seed = horizon, dt, speed_mps, rot_speed, seed
        self._noise: dict[int, np.ndarray] = {}

    # ---- per-episode perception error (same z-draw for every profile)
    def episode_z(self, scene_seq: int) -> np.ndarray:
        z = self._noise.get(scene_seq)
        if z is None:
            h = int.from_bytes(hashlib.sha256(f"{self.seed}:{scene_seq}".encode()).digest()[:8], "big")
            z = np.random.default_rng(h).standard_normal(5)
            if len(self._noise) > 512:
                self._noise.clear()
            self._noise[scene_seq] = z
        return z

    # ---- the plan
    def plan(self, state: Sequence[float], scene: dict[str, Any], profile: NoiseProfile) -> tuple[np.ndarray, dict[str, Any]]:
        q = np.asarray(state[:6], dtype=float)
        g = float(state[6]) if len(state) > 6 else 1.0
        cube = scene.get("pick") or scene["cube"]  # the task's object (scene spec success.object)
        place = scene.get("place") or scene["plate"]  # where it goes (success.target / region)
        task = scene.get("task") or {}
        z = self.episode_z(int(scene.get("scene_seq") or 0))
        cx, cy, cz = cube["xyz"]
        half = float(cube.get("half", 0.02))
        px, py = place["xy"]
        top = float(place["top_z"])
        # perceived poses (the injected error)
        pcx, pcy = cx + profile.pos_sigma_m * z[0], cy + profile.pos_sigma_m * z[1]
        pyaw = float(cube["yaw"]) + profile.yaw_sigma_rad * z[2]
        ppx, ppy = px + profile.place_sigma_m * z[3], py + profile.place_sigma_m * z[4]

        t_ee = self.kin.fk(q)
        ee = t_ee[:3, 3]
        held = bool(task.get("held")) and g < 0.85
        grasp_z = half + 0.002
        pre_z = 0.10
        carry_z = 0.14
        place_z = top + half + 0.022
        boxy = cube.get("primitive", "box") == "box"  # round objects: any wrist roll works
        phi_cube = grasp_heading(pcx, pcy, pyaw) if boxy else math.atan2(pcy, pcx) + math.pi / 2
        phi_plate = math.atan2(ppy, ppx) + math.pi / 2
        gripper = 1.0
        hold_first = 0

        if task.get("success"):
            phase, target, gripper = "retreat", tool_pose(ppx, ppy, 0.20, phi_plate), 1.0
        elif held:
            dxy_plate = math.hypot(ee[0] - ppx, ee[1] - ppy)
            lowering = ee[2] < carry_z - 0.02  # hysteresis: once lowering, small xy drift is fine
            if ee[2] < half + 0.03 and math.hypot(ee[0] - cx, ee[1] - cy) < 0.03:
                phase, target, gripper, hold_first = "lift", tool_pose(ee[0], ee[1], carry_z, phi_cube), 0.0, 6
            elif dxy_plate > (0.03 if lowering else 0.012):
                phase = "carry"
                target = tool_pose(ppx, ppy, carry_z, phi_plate if dxy_plate < 0.25 else phi_cube)
                gripper = 0.0
            elif ee[2] > place_z + 0.008:
                phase, target, gripper = "lower", tool_pose(ppx, ppy, place_z, phi_plate), 0.0
            else:
                phase, target, gripper = "release", tool_pose(ppx, ppy, place_z, phi_plate), 1.0
        elif g < 0.15 and ee[2] < pre_z + 0.03:
            phase, target, gripper = "regrasp", tool_pose(ee[0], ee[1], pre_z + 0.02, phi_cube), 1.0
        else:
            dxy = math.hypot(ee[0] - pcx, ee[1] - pcy)
            at_grasp = dxy < 0.008 and abs(ee[2] - grasp_z) < 0.008
            if at_grasp or (g < 0.9 and dxy < 0.012 and ee[2] < grasp_z + 0.015):
                phase, target, gripper = "close", tool_pose(pcx, pcy, grasp_z, phi_cube), 0.0
            elif dxy < (0.03 if ee[2] < pre_z - 0.015 else 0.012) and ee[2] < pre_z + 0.02:
                phase, target = "descend", tool_pose(pcx, pcy, grasp_z, phi_cube)
            else:
                phase, target = "approach", tool_pose(pcx, pcy, pre_z, phi_cube)
        acts = self._line(q, t_ee, target, gripper, hold_first)
        info = {"phase": phase, "perceived_cube_xy": [round(pcx, 4), round(pcy, 4)],
                "error_cm": round(math.hypot(pcx - cx, pcy - cy) * 100, 2), **profile.describe()}
        return acts, info

    def _line(self, q: np.ndarray, t0: np.ndarray, t1: np.ndarray, gripper: float, hold_first: int = 0) -> np.ndarray:
        """Straight Cartesian line t0 -> t1 at the planner speeds, then hold; joints via IK every 5 steps."""
        h = self.horizon
        dist = float(np.linalg.norm(t1[:3, 3] - t0[:3, 3]))
        from .adapters.yam_kinematics import rotvec

        ang = float(np.linalg.norm(rotvec(t1[:3, :3] @ t0[:3, :3].T)))
        dur = max(dist / self.speed, ang / self.rot_speed, 0.2)
        n_move = max(1, int(math.ceil(dur / self.dt)))
        key_steps = sorted(set(list(range(0, h, 5)) + [h - 1]))
        keys: dict[int, np.ndarray] = {}
        seed = q.copy()
        for k in key_steps:
            i = max(0, k + 1 - hold_first)
            a = min(1.0, i / n_move)
            a = a * a * (3 - 2 * a)
            t = np.eye(4)
            t[:3, :3] = _slerp_rot(t0[:3, :3], t1[:3, :3], a)
            t[:3, 3] = t0[:3, 3] + (t1[:3, 3] - t0[:3, 3]) * a
            try:
                seed = self.kin.ik(t, seed, extra_seeds=[HOME], max_iters=80)
            except Exception:  # noqa: BLE001 - unreachable waypoint: hold the last good one
                pass
            keys[k] = seed.copy()
        out = np.zeros((h, 7))
        ks = sorted(keys)
        for i in range(h):
            k1 = next(k for k in ks if k >= i)
            k0 = max(k for k in ks if k <= i)
            w = 0.0 if k1 == k0 else (i - k0) / (k1 - k0)
            qi = keys[k0] + (keys[k1] - keys[k0]) * w
            if i < hold_first:
                qi = q
            out[i, :6] = qi
        out[:, 6] = gripper
        return out


__all__ = ["HOME", "POLICY_LABEL", "POLICY_NAME", "PROFILES", "NoiseProfile", "ScriptedPickPlace", "grasp_heading",
           "profile_for", "tool_pose"]
