"""[SIM] Scripted IK pick-and-place policy for the MuJoCo YAM demo. NOT pi0.5, not learned.

A stand-in for the policy server when there is no GPU: it plans the scene's pick-and-place task ("pick up
the cube and place it in the bin": the TDM-104 scene spec's ``success`` object and target) with inverse
kinematics and returns the same 50-step ``joint_pos_abs`` action chunks a fine-tuned YAM checkpoint returns
(TDM-41 wire format). ``sim_policy_server.py`` serves it.

How it decides (stateless per request, like a learned policy: the arm state comes in the request):
  * the robot state (6 joints + gripper) from the request, and PRIVILEGED sim state from the sim
    robot's ``/sim/state``: the scene spec's task object (``pick``), where it goes (``place``),
    finger contacts and task status (it does not look at the images; a real policy would);
  * phases: approach above the object -> descend -> close -> lift -> carry above the target -> lower ->
    release -> retreat. A grasp that closes on nothing re-opens and tries again;
  * every height comes from the scene spec's geometry (``task_geometry``): the grasp height and the pre-grasp
    clearance from the picked object's real size (cube, block, ball, cup); the carry height clears the
    target's walls (a plate / bin / bowl rim) and anything near the carry path; the release height is just
    above the floor the object will rest on (the plate or bin floor, a box top for stacking, the table for a
    region), whatever the target is. Phases are decided from where the tool is relative to THOSE heights, so a
    target whose floor is the table (a region) no longer loops between "lower" and "lift";
  * each phase is a straight Cartesian line at ``speed_mps`` with the tool tilted 20 deg from
    vertical (a straight-down tool puts the YAM's joint4 on its limit), the fingers aligned with
    the nearest cube faces; IK is TDM-46's PoE model of the YAM.

Why it fails sometimes: an injected per-episode PERCEPTION ERROR on where it thinks the cube is
(and where the plate is), drawn once per scene reset (``scene_seq``) and scaled by the active
checkpoint's profile. The same standard-normal draw is used for every profile, so baseline and
"fine-tuned" see the same scenes and differ only in the error size:

  * ``baseline``  (the base checkpoint, ``gs://openpi-assets/checkpoints/pi05_base`` / ``base``):
    sigma 2.2 cm, yaw 0.3 rad  ->  misses a meaningful share of grasps (calibrated: see tests);
  * ``finetuned`` (any other checkpoint URI, e.g. the stub trainer's): sigma 1.0 cm, yaw 0.12 rad.

The noise is the same on every scene; ``spikes/mujoco-sim/calibrate_sim_policy.py --scene ...`` measures the
rates per scene (e.g. cube_plate and a generated cube_bin).

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


TABLE_CLEAR = 0.012  # the grasp point stays this far above the table (fingertips)
GRASP_ABOVE_CENTRE = 0.002  # grasp height = the object's centre + this
PRE_GRASP_Z, PRE_GRASP_CLEAR = 0.10, 0.05  # approach height: >= 10 cm, and >= 5 cm above the object's top
CARRY_Z, CARRY_CLEAR, CARRY_MAX = 0.14, 0.03, 0.25  # carry: >= 14 cm; the object's bottom >= 3 cm over walls
RELEASE_GAP = 0.02  # release with the object's bottom this far above the floor it lands on
RETREAT_Z = 0.20
RIM_HEIGHT = 0.009  # a plate's rim above its surface (adapters/mujoco_scene.PLATE_RIM_HEIGHT)
OPEN_WIDTH = 0.095  # m between the pads at gripper 1.0 (2 x FINGER_TRAVEL)
PATH_MARGIN = 0.04  # an object this close to the carry path (plus both footprints) counts as an obstacle


@dataclass(frozen=True)
class TaskGeometry:
    """Where the task's object is, how big it is, and where / how high it goes: read from the scene spec
    (TDM-104 ``rdf_common.scene_spec``) and the sim's live poses. All heights are world z (m)."""

    pick_id: str
    pick_xyz: tuple[float, float, float]
    pick_yaw: float
    pick_half: float  # half height of the picked object
    pick_width: float  # narrowest horizontal width (what the fingers close across)
    pick_boxy: bool  # a box: align the fingers with its faces (round objects: any wrist roll)
    pick_long_axis: float | None  # a non-square box: the yaw of its LONG side (close across the short one)
    place_id: str
    place_kind: str  # "container" (plate / bin / bowl), "surface" (a box / cylinder top), "region" (the table)
    place_xy: tuple[float, float]
    place_room: float  # the object's centre must end within this radius of place_xy (success margin applied)
    floor_z: float  # the surface the object comes to rest on
    wall_z: float  # the highest rim / wall around the place (>= floor_z)
    clear_z: float  # the highest thing the carried object passes over (walls + obstacles near the path)
    table_z: float = 0.0
    pick_rest_z: float | None = None  # the object's centre z resting where it spawns (the spec pose)

    @property
    def lift_below(self) -> float:
        """Holding it with the tool below this (away from the target) = just grasped: lift straight up first.
        From the RESTING height: the live z rides up with the gripper once it is held."""
        rest = self.pick_rest_z if self.pick_rest_z is not None else self.pick_xyz[2]
        return max(rest, self.table_z + TABLE_CLEAR) + GRASP_ABOVE_CENTRE + 0.03

    @property
    def grasp_z(self) -> float:
        return max(self.pick_xyz[2], self.table_z + TABLE_CLEAR) + GRASP_ABOVE_CENTRE

    @property
    def pre_z(self) -> float:
        return max(PRE_GRASP_Z, self.pick_xyz[2] + self.pick_half + PRE_GRASP_CLEAR)

    @property
    def carry_z(self) -> float:
        # held at its centre (+2 mm): the object's bottom is at tool z - half - 2 mm
        return min(max(CARRY_Z, self.clear_z + self.pick_half + GRASP_ABOVE_CENTRE + CARRY_CLEAR), CARRY_MAX)

    @property
    def place_z(self) -> float:
        return self.floor_z + self.pick_half + GRASP_ABOVE_CENTRE + RELEASE_GAP

    @property
    def empty_g(self) -> float:
        """Gripper opening below which the fingers closed on nothing (holding the object keeps them ~w/0.095)."""
        return min(0.15, 0.5 * self.pick_width / OPEN_WIDTH)

    def describe(self) -> dict[str, Any]:
        return {"pick": self.pick_id, "place": self.place_id, "place_kind": self.place_kind,
                "grasp_z": round(self.grasp_z, 4), "pre_z": round(self.pre_z, 4), "carry_z": round(self.carry_z, 4),
                "place_z": round(self.place_z, 4), "floor_z": round(self.floor_z, 4), "wall_z": round(self.wall_z, 4)}


def _seg_dist(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    """Horizontal distance from point ``p`` to the segment ``a``-``b``."""
    ax, ay, bx, by, px, py = a[0], a[1], b[0], b[1], p[0], p[1]
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 < 1e-12 else min(1.0, max(0.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def task_geometry(scene: dict[str, Any]) -> TaskGeometry:
    """The task's geometry from the sim robot's ``/sim/state``: its ``spec`` (the TDM-104 scene spec) plus the
    live object poses (``objects``). A state without a spec (an older sim) falls back to ``pick`` / ``place``."""
    spec = scene.get("spec") if isinstance(scene.get("spec"), dict) else None
    if not spec or not (spec.get("success") or {}).get("object"):
        return _legacy_geometry(scene)
    from rdf_common import scene_spec as ss

    objs = {o["id"]: o for o in spec.get("objects") or []}
    live = scene.get("objects") or {}
    succ = spec["success"]
    table_z = float((spec.get("table") or {}).get("top_z") or 0.0)

    def pos(oid: str) -> list[float]:
        v = (live.get(oid) or {}).get("xyz")
        return [float(x) for x in v] if v else [float(x) for x in objs[oid]["pose"]["pos"]]

    def top(oid: str) -> float:  # the highest point of the object (a plate: its rim)
        o = objs[oid]
        return ss.top_z(o, pos(oid)[2]) + (RIM_HEIGHT if o["primitive"] == "plate" else 0.0)

    pid = succ["object"]
    po = objs[pid]
    pxyz = pos(pid)
    pyaw = float((live.get(pid) or {}).get("yaw", po["pose"].get("yaw", 0.0)) or 0.0)
    long_axis = None
    if po["primitive"] == "box":
        sx, sy = float(po["size"][0]), float(po["size"][1])
        if abs(sx - sy) > 0.1 * min(sx, sy):
            long_axis = pyaw if sx > sy else pyaw + math.pi / 2
    margin = float(succ.get("margin", 0.012))
    tgt, reg = succ.get("target"), succ.get("region")
    if tgt:
        t = objs[tgt]
        tx, ty, tz = pos(tgt)
        floor = ss.top_z(t, tz)  # a plate / bin / bowl: its surface; a box or cylinder: its top face
        kind = "container" if t["primitive"] == "plate" else "surface"
        wall = floor + (RIM_HEIGHT if kind == "container" else 0.0)
        room = max(ss.inner_radius(t) - margin, 0.004)
        outer = ss.footprint_radius(t)
        place_id, place_xy = str(tgt), (tx, ty)
    else:  # a region drawn on the table: the floor is the table
        r = spec["regions"][reg]
        place_xy = ((r["x"][0] + r["x"][1]) / 2, (r["y"][0] + r["y"][1]) / 2)
        floor = wall = table_z
        kind = "region"
        room = max(min(r["x"][1] - r["x"][0], r["y"][1] - r["y"][0]) / 2 - margin, 0.004)
        outer = math.hypot(r["x"][1] - r["x"][0], r["y"][1] - r["y"][0]) / 2
        place_id = str(reg)
    clear = wall
    for oid, o in objs.items():
        if oid in (pid, tgt):
            continue
        ox, oy, oz = pos(oid)
        d = math.hypot(ox - place_xy[0], oy - place_xy[1])
        if not o.get("dynamic") and d < ss.inner_radius(o) and ss.top_z(o, oz) < floor + 0.06:
            floor = max(floor, ss.top_z(o, oz))  # a fixed floor under the goal (a tray in a region)
            wall = max(wall, top(oid))
        elif d < outer + ss.footprint_radius(o) + 0.02:
            wall = max(wall, top(oid))  # a wall / rim / neighbour around the goal
        elif _seg_dist((ox, oy), pxyz, place_xy) < ss.footprint_radius(o) + ss.footprint_radius(po) + PATH_MARGIN:
            clear = max(clear, top(oid))  # in the way of the carry
    wall = max(wall, floor)
    return TaskGeometry(pick_id=str(pid), pick_xyz=(pxyz[0], pxyz[1], pxyz[2]), pick_yaw=pyaw,
                        pick_half=ss.half_height(po), pick_width=ss.grip_width(po), pick_boxy=po["primitive"] == "box",
                        pick_long_axis=long_axis, place_id=place_id, place_kind=kind, place_xy=place_xy,
                        place_room=room, floor_z=floor, wall_z=wall, clear_z=max(clear, wall), table_z=table_z,
                        pick_rest_z=float(po["pose"]["pos"][2]))


def _legacy_geometry(scene: dict[str, Any]) -> TaskGeometry:
    """An older sim's ``/sim/state`` (no ``spec``): the pick / place summary only."""
    cube = scene.get("pick") or scene["cube"]
    place = scene.get("place") or scene["plate"]
    half = float(cube.get("half", 0.02))
    top = float(place["top_z"])
    xyz = [float(v) for v in cube["xyz"]]
    return TaskGeometry(pick_id=str(cube.get("id") or "cube"), pick_xyz=(xyz[0], xyz[1], xyz[2]),
                        pick_yaw=float(cube.get("yaw", 0.0)), pick_half=half, pick_width=2 * half,
                        pick_boxy=cube.get("primitive", "box") == "box", pick_long_axis=None,
                        place_id=str(place.get("id") or "plate"), place_kind="container",
                        place_xy=(float(place["xy"][0]), float(place["xy"][1])),
                        place_room=float(place.get("radius", 0.075)) - 0.012, floor_z=top, wall_z=top + RIM_HEIGHT,
                        clear_z=top + RIM_HEIGHT, table_z=float(scene.get("table_top") or 0.0), pick_rest_z=half)


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


def grasp_heading(x: float, y: float, cube_yaw: float, long_axis: float | None = None) -> float:
    """Closing-axis heading aligned with a cube face, nearest the arm's natural wrist roll. For a box that is
    longer one way (``long_axis``: the yaw of its long side) the fingers close across the SHORT side."""
    natural = math.atan2(y, x) + math.pi / 2
    if long_axis is not None:
        across = long_axis + math.pi / 2
        return across + round((natural - across) / math.pi) * math.pi
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
        geo = task_geometry(scene)  # the scene spec's object + target geometry, live poses
        task = scene.get("task") or {}
        z = self.episode_z(int(scene.get("scene_seq") or 0))
        cx, cy, _ = geo.pick_xyz
        px, py = geo.place_xy
        # perceived poses (the injected error)
        pcx, pcy = cx + profile.pos_sigma_m * z[0], cy + profile.pos_sigma_m * z[1]
        pyaw = geo.pick_yaw + profile.yaw_sigma_rad * z[2]
        long_axis = None if geo.pick_long_axis is None else geo.pick_long_axis + profile.yaw_sigma_rad * z[2]
        ppx, ppy = px + profile.place_sigma_m * z[3], py + profile.place_sigma_m * z[4]

        t_ee = self.kin.fk(q)
        ee = t_ee[:3, 3]
        held = bool(task.get("held")) and g < 0.85
        grasp_z, pre_z, carry_z, place_z = geo.grasp_z, geo.pre_z, geo.carry_z, geo.place_z
        # round objects: any wrist roll works
        phi_cube = grasp_heading(pcx, pcy, pyaw, long_axis) if geo.pick_boxy else math.atan2(pcy, pcx) + math.pi / 2
        phi_plate = math.atan2(ppy, ppx) + math.pi / 2
        gripper = 1.0
        hold_first = 0

        if task.get("success"):
            phase, target, gripper = "retreat", tool_pose(ppx, ppy, max(RETREAT_Z, carry_z), phi_plate), 1.0
        elif held:
            dxy_plate = math.hypot(ee[0] - ppx, ee[1] - ppy)
            lowering = ee[2] < carry_z - 0.02  # hysteresis: once lowering, small xy drift is fine
            over = dxy_plate <= (0.03 if lowering else 0.012)
            if not over and ee[2] < geo.lift_below:
                # just closed on it, low: straight up first. Only AWAY from the target: over the target a low
                # tool means lowering / releasing, however low the target's floor is (a region on the table
                # puts the release height below this lift band, and the old "near the object" test, always
                # true while holding it, looped lower -> lift -> lower until the time limit)
                phase, target, gripper, hold_first = "lift", tool_pose(ee[0], ee[1], carry_z, phi_cube), 0.0, 6
            elif not over:
                phase = "carry"
                target = tool_pose(ppx, ppy, carry_z, phi_plate if dxy_plate < 0.25 else phi_cube)
                gripper = 0.0
            elif ee[2] > place_z + 0.008:
                phase, target, gripper = "lower", tool_pose(ppx, ppy, place_z, phi_plate), 0.0
            else:
                phase, target, gripper = "release", tool_pose(ppx, ppy, place_z, phi_plate), 1.0
        elif g < geo.empty_g and ee[2] < pre_z + 0.03:
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
                "error_cm": round(math.hypot(pcx - cx, pcy - cy) * 100, 2), "geometry": geo.describe(),
                **profile.describe()}
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


__all__ = ["HOME", "POLICY_LABEL", "POLICY_NAME", "PROFILES", "NoiseProfile", "ScriptedPickPlace", "TaskGeometry",
           "grasp_heading", "profile_for", "task_geometry", "tool_pose"]
