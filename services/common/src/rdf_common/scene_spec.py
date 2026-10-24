"""Scene spec v1 (TDM-104): ONE description of a robot task scene, read by the MuJoCo sim and the three.js twin.

A ticket's scene is a JSON document. The orchestrator generates it from the task text (Claude, through
``rdf_common.llm``), checks it here, stores it per ticket in event_log (``scene.created``) and deploys it to
the ticket's SIM robot (``scene.deployed``). The robot-client builds its MJCF from it
(``rdf_robot_client.adapters.mujoco_scene.load_scene``), and the web renders the same document
(``apps/web/src/lib/robotview/scene.ts``, ``toRenderSpec``). So the cube you see in the browser is the cube
MuJoCo simulates.

    from rdf_common import scene_spec as ss
    spec = ss.validate(raw)                    # normalized dict, or raises SceneSpecError (all problems listed)
    ok = ss.evaluate_success(spec, poses, held={"cube"})["success"]
    ss.describe_success(spec)                  # "inside(cube, plate)"

Frames and units: metres, radians, RGBA 0..1. The world frame is the robot BASE frame (x forward, y left,
z up); the table top is ``table.top_z`` (0: the arm sits on the table). Quaternions are [w, x, y, z].

Shape (the MuJoCo adapter's format, which this module owns from now on)::

  {"version": 1, "name": "cube_plate", "task": "Pick up the cube and place it in the empty plate.",
   "notes"?: str, "physics"?: {"timestep": 0.002},
   "robot"?: {"model": "yam_linear_4310", "mount": {"pos": [0, 0, 0], "yaw": 0}},   # v1: identity only
   "table": {"center": [x, y], "size": [sx, sy], "top_z": 0.0, "color": [r, g, b, a]},
   "objects": [{"id", "label"?, "primitive": box|cylinder|sphere|plate,
                "size": [sx, sy, sz]            (box: FULL extents)
                "radius", "height"              (cylinder / plate: full height; sphere: radius only)
                "color", "rim_color"? (plate), "mass"? kg, "friction"? [slide, spin, roll],
                "dynamic": bool (free joint) , "graspable"?: bool,
                "pose": {"pos": [x, y, z], "yaw": rad},   # z = centre (plate: its bottom); snapped to rest
                "randomize"?: {"region": <region name>, "yaw": [lo, hi]}}],
   "regions": {<name>: {"x": [lo, hi], "y": [lo, hi], "color"?: rgba, "visible"?: bool}},
   "cameras": {"top": {"pos", "target", "fovy", "width"?, "height"?},
               "wrist": {"mount": "wrist_d405", "fovy", ...}, <other world cameras>...},
   "success": {"type": "object_in" | "object_on", "object": <id>,
               "target": <object id> | "region": <region name>, "margin"?: m, "released"?: bool},
   "meta"?: {"ticket_id", "revision", "scene_id", "source", ...}}      # set by the store, not the author

Success predicate (``evaluate_success``): the ``object``'s centre is over the target (``object_in``: within
the target's radius minus ``margin``, default 0.012 m; ``object_on``: over the target's top face) and resting
on it (z = target top + the object's half height, +-1.5 cm), and, when ``released`` (default true), the
gripper is not holding it. A region target is a rectangle on the table. Spoken form: ``inside(cube, plate)``
/ ``on_top(red_block, blue_block)``.

``validate`` normalizes as well as checks: colors given as "#rrggbb" become RGBA, missing defaults are filled
(table, cameras, physics, robot), and every object's z is snapped so it rests on what is under it (the table,
a plate, or the object it is stacked on). Each change is listed in ``spec["meta"]["fixes"]``.
"""
from __future__ import annotations

import copy
import math
import random
import re
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1
PRIMITIVES = ("box", "cylinder", "sphere", "plate")
SUCCESS_TYPES = ("object_in", "object_on")
WRIST_MOUNTS = ("wrist_d405",)
ROBOT_MODEL = "yam_linear_4310"
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$")

# The real rig (and the sim): a 96 x 96 cm table centred 30 cm in front of the base, top at the base plane.
DEFAULT_TABLE = {"center": [0.30, 0.0], "size": [0.96, 0.96], "top_z": 0.0, "color": [0.62, 0.50, 0.37, 1.0]}
DEFAULT_CAMERAS = {
    "top": {"pos": [0.86, -0.50, 0.58], "target": [0.27, 0.03, 0.06], "fovy": 52, "width": 640, "height": 480},
    "wrist": {"mount": "wrist_d405", "fovy": 72, "width": 640, "height": 480},
}
DEFAULT_PHYSICS = {"timestep": 0.002}
DEFAULT_ROBOT = {"model": ROBOT_MODEL, "mount": {"pos": [0.0, 0.0, 0.0], "yaw": 0.0}}
DEFAULT_MARGIN = {"object_in": 0.012, "object_on": 0.0}

# Physical envelope of the YAM + linear_4310 gripper on this table (checked by validate).
REACH_MIN, REACH_MAX = 0.15, 0.55       # m, horizontal distance of anything the arm must touch from the base
GRIP_MAX_WIDTH = 0.080                  # m, pads open to 9.4 cm; keep a margin for the approach
GRIP_MIN_WIDTH = 0.012
MAX_GRASP_MASS = 0.5                    # kg, friction grasp
SIZE_MIN, SIZE_MAX = 0.01, 0.40         # m, any object dimension
MAX_OBJECTS = 10
REST_TOL = 0.015                        # m, "resting on" tolerance for the success predicate
SNAP_TOL = 0.002                        # m, z further off than this from resting is snapped (and logged)

TOP_KEYS = {"version", "name", "task", "notes", "physics", "robot", "table", "objects", "regions", "cameras",
            "success", "meta"}
OBJECT_KEYS = {"id", "label", "primitive", "size", "radius", "height", "color", "rim_color", "mass", "friction",
               "dynamic", "graspable", "pose", "randomize"}
REGION_KEYS = {"x", "y", "color", "visible", "label"}
SUCCESS_KEYS = {"type", "object", "target", "region", "margin", "released"}


class SceneSpecError(ValueError):
    """The spec is not usable. ``.errors`` lists every problem (one line each, with its JSON path)."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("invalid scene spec: " + "; ".join(self.errors[:12])
                         + (f" (+{len(self.errors) - 12} more)" if len(self.errors) > 12 else ""))


# ------------------------------------------------------------------ small helpers

def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _vec(v: Any, n: int) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == n and all(_num(x) for x in v)


def color_rgba(c: Any) -> list[float] | None:
    """'#rrggbb' / '#rrggbbaa' / [r, g, b] / [r, g, b, a] (0..1) -> [r, g, b, a]; None if unreadable."""
    if isinstance(c, str):
        m = HEX_RE.match(c.strip())
        if not m:
            return None
        h = m.group(1) + (m.group(2) or "ff")
        return [round(int(h[i:i + 2], 16) / 255, 4) for i in (0, 2, 4, 6)]
    if isinstance(c, (list, tuple)) and len(c) in (3, 4) and all(_num(x) and 0 <= x <= 1 for x in c):
        return [float(x) for x in c] + ([1.0] if len(c) == 3 else [])
    return None


def color_hex(rgba: Iterable[float]) -> str:
    r = list(rgba)
    return "#" + "".join(f"{max(0, min(255, round(float(x) * 255))):02x}" for x in r[:3])


def half_height(o: Mapping[str, Any]) -> float:
    p = o["primitive"]
    if p == "box":
        return float(o["size"][2]) / 2
    if p == "sphere":
        return float(o["radius"])
    if p == "plate":
        return float(o["height"])  # a plate is anchored at its bottom: "half height" = full height above pos
    return float(o["height"]) / 2


def footprint_radius(o: Mapping[str, Any]) -> float:
    """Circumscribed radius of the object's footprint (conservative, for overlap tests)."""
    if o["primitive"] == "box":
        return math.hypot(float(o["size"][0]), float(o["size"][1])) / 2
    return float(o["radius"])


def inner_radius(o: Mapping[str, Any]) -> float:
    """Inscribed radius of the footprint (what an object resting on it must stay inside)."""
    if o["primitive"] == "box":
        return min(float(o["size"][0]), float(o["size"][1])) / 2
    return float(o["radius"])


def grip_width(o: Mapping[str, Any]) -> float:
    """Narrowest horizontal width the gripper could close across."""
    if o["primitive"] == "box":
        return min(float(o["size"][0]), float(o["size"][1]))
    return 2 * float(o["radius"])


def top_z(o: Mapping[str, Any], z: float | None = None) -> float:
    """Height of the object's upper surface with its centre (plate: bottom) at ``z`` (default: its pose)."""
    z = float(o["pose"]["pos"][2]) if z is None else z
    if o["primitive"] == "plate":
        return z + float(o["height"])
    return z + half_height(o)


def yaw_quat(yaw: float) -> list[float]:
    return [math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)]


def _objects(spec: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {o["id"]: o for o in spec.get("objects") or []}


def slugify(text: str, default: str = "scene") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:32].strip("_")
    return s if s and s[0].isalpha() else default


# ------------------------------------------------------------------ validate + normalize

def validate(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Check ``raw`` and return the normalized spec (a new dict). Raises ``SceneSpecError`` listing every problem."""
    errs: list[str] = []
    fixes: list[str] = []
    if not isinstance(raw, Mapping):
        raise SceneSpecError(["$: a scene spec is a JSON object"])
    spec: dict[str, Any] = copy.deepcopy(dict(raw))
    for k in sorted(set(spec) - TOP_KEYS):
        errs.append(f"$.{k}: unknown key (allowed: {', '.join(sorted(TOP_KEYS))})")
    if spec.get("version") != SCHEMA_VERSION:
        errs.append(f"$.version: must be {SCHEMA_VERSION}")
    task = spec.get("task")
    if not isinstance(task, str) or not task.strip():
        errs.append("$.task: the task sentence is required")
    name = spec.get("name")
    if name is None:
        spec["name"] = slugify(task if isinstance(task, str) else "")
    elif not isinstance(name, str) or not ID_RE.match(name):
        errs.append("$.name: lowercase slug [a-z][a-z0-9_]{0,31}")

    # robot: v1 = the YAM on the table at the world origin (the sim's world frame IS the base frame)
    robot = spec.setdefault("robot", copy.deepcopy(DEFAULT_ROBOT))
    if not isinstance(robot, dict):
        errs.append("$.robot: object")
    else:
        robot.setdefault("model", ROBOT_MODEL)
        mount = robot.setdefault("mount", copy.deepcopy(DEFAULT_ROBOT["mount"]))
        if robot["model"] != ROBOT_MODEL:
            errs.append(f"$.robot.model: v1 supports only {ROBOT_MODEL}")
        if not isinstance(mount, dict) or not _vec(mount.get("pos", [0, 0, 0]), 3) or \
                any(abs(v) > 1e-6 for v in mount.get("pos", [0, 0, 0])) or abs(float(mount.get("yaw", 0) or 0)) > 1e-6:
            errs.append("$.robot.mount: v1 requires the arm at the world origin (pos [0,0,0], yaw 0): "
                        "world frame == robot base frame")

    phys = spec.setdefault("physics", dict(DEFAULT_PHYSICS))
    if not isinstance(phys, dict) or not (_num(phys.get("timestep", 0.002)) and 0.0005 <= phys.get("timestep", 0.002) <= 0.005):
        errs.append("$.physics.timestep: 0.0005..0.005 s")

    # table
    table = spec.get("table")
    if table is None:
        spec["table"] = table = copy.deepcopy(DEFAULT_TABLE)
    if not isinstance(table, dict):
        errs.append("$.table: object")
        table = spec["table"] = copy.deepcopy(DEFAULT_TABLE)
    for k, v in DEFAULT_TABLE.items():
        if k not in table:
            table[k] = copy.deepcopy(v)
    if not _vec(table["center"], 2):
        errs.append("$.table.center: [x, y]")
    if not _vec(table["size"], 2) or not all(0.2 <= v <= 2.0 for v in table["size"]):
        errs.append("$.table.size: [sx, sy], 0.2..2.0 m")
    if not _num(table["top_z"]) or abs(table["top_z"]) > 0.3:
        errs.append("$.table.top_z: number within +-0.3 m of the base")
    tc = color_rgba(table["color"])
    if tc is None:
        errs.append("$.table.color: '#rrggbb' or RGBA 0..1")
    else:
        table["color"] = tc
    if errs and any(e.startswith("$.table") for e in errs):
        raise SceneSpecError(errs)
    t_top = float(table["top_z"])
    tx0, ty0 = table["center"][0] - table["size"][0] / 2, table["center"][1] - table["size"][1] / 2
    tx1, ty1 = table["center"][0] + table["size"][0] / 2, table["center"][1] + table["size"][1] / 2

    # regions
    regions = spec.get("regions")
    if regions is None:
        regions = spec["regions"] = {}
    if isinstance(regions, list):  # the LLM form: [{"name", ...}]
        conv: dict[str, Any] = {}
        for r in regions:
            if isinstance(r, dict) and isinstance(r.get("name"), str):
                conv[r["name"]] = {k: v for k, v in r.items() if k != "name" and v is not None}
            else:
                errs.append("$.regions[]: each needs a name")
        regions = spec["regions"] = conv
    if not isinstance(regions, dict):
        errs.append("$.regions: object {name: {x: [lo, hi], y: [lo, hi]}}")
        regions = spec["regions"] = {}
    for rn, r in regions.items():
        p = f"$.regions.{rn}"
        if not ID_RE.match(str(rn)):
            errs.append(f"{p}: name must be a lowercase slug")
        if not isinstance(r, dict):
            errs.append(f"{p}: object")
            continue
        for k in sorted(set(r) - REGION_KEYS):
            errs.append(f"{p}.{k}: unknown key")
        for ax, lo_b, hi_b in (("x", tx0, tx1), ("y", ty0, ty1)):
            v = r.get(ax)
            if not _vec(v, 2) or not v[0] < v[1]:
                errs.append(f"{p}.{ax}: [lo, hi] with lo < hi")
            elif v[0] < lo_b - 1e-9 or v[1] > hi_b + 1e-9:
                errs.append(f"{p}.{ax}: {v} leaves the table ({lo_b:.3f}..{hi_b:.3f})")
        if "color" in r:
            rc = color_rgba(r["color"])
            if rc is None:
                errs.append(f"{p}.color: '#rrggbb' or RGBA")
            else:
                r["color"] = rc

    # objects
    objs_raw = spec.get("objects")
    if not isinstance(objs_raw, list) or not objs_raw:
        errs.append("$.objects: a non-empty list")
        objs_raw = []
    if len(objs_raw) > MAX_OBJECTS:
        errs.append(f"$.objects: at most {MAX_OBJECTS}")
    objs: dict[str, dict[str, Any]] = {}
    good: list[dict[str, Any]] = []
    for i, o in enumerate(objs_raw):
        p = f"$.objects[{i}]"
        if not isinstance(o, dict):
            errs.append(f"{p}: object")
            continue
        for k in [k for k, v in o.items() if v is None]:  # the LLM form sends null for "not applicable"
            del o[k]
        oid = o.get("id")
        if not isinstance(oid, str) or not ID_RE.match(oid):
            errs.append(f"{p}.id: lowercase slug [a-z][a-z0-9_]{{0,31}}")
            continue
        p = f"$.objects[{oid}]"
        if oid in objs:
            errs.append(f"{p}: duplicate id")
            continue
        for k in sorted(set(o) - OBJECT_KEYS):
            errs.append(f"{p}.{k}: unknown key")
        prim = o.get("primitive")
        bad = len(errs)
        if prim not in PRIMITIVES:
            errs.append(f"{p}.primitive: one of {', '.join(PRIMITIVES)}")
        elif prim == "box":
            if not _vec(o.get("size"), 3) or not all(SIZE_MIN <= v <= SIZE_MAX for v in o["size"]):
                errs.append(f"{p}.size: a box needs size [sx, sy, sz] (full extents, {SIZE_MIN}..{SIZE_MAX} m)")
            o.pop("radius", None), o.pop("height", None)
        else:
            if not _num(o.get("radius")) or not SIZE_MIN / 2 <= o["radius"] <= SIZE_MAX / 2:
                errs.append(f"{p}.radius: {SIZE_MIN / 2}..{SIZE_MAX / 2} m")
            if prim in ("cylinder", "plate"):
                lo_h = 0.005 if prim == "plate" else SIZE_MIN
                hi_h = 0.05 if prim == "plate" else SIZE_MAX
                if not _num(o.get("height")) or not lo_h <= o["height"] <= hi_h:
                    errs.append(f"{p}.height: {lo_h}..{hi_h} m (full height)")
            else:
                o.pop("height", None)
            o.pop("size", None)
        for ck in ("color", "rim_color"):
            if ck in o:
                cc = color_rgba(o[ck])
                if cc is None:
                    errs.append(f"{p}.{ck}: '#rrggbb' or RGBA 0..1")
                else:
                    o[ck] = cc
        if "color" not in o:
            o["color"] = [0.7, 0.7, 0.7, 1.0]
        if prim == "plate":
            o["dynamic"] = bool(o.get("dynamic", False))
            if o["dynamic"]:
                errs.append(f"{p}.dynamic: plates are fixtures (dynamic false)")
        elif not isinstance(o.get("dynamic", True), bool):
            errs.append(f"{p}.dynamic: bool")
        else:
            o["dynamic"] = o.get("dynamic", True)
        if "graspable" in o and not isinstance(o["graspable"], bool):
            errs.append(f"{p}.graspable: bool")
        if "mass" in o and (not _num(o["mass"]) or not 0.005 <= o["mass"] <= 2.0):
            errs.append(f"{p}.mass: 0.005..2 kg")
        if "friction" in o:
            f = o["friction"]
            if _num(f):
                f = o["friction"] = [float(f), 0.01, 0.001]
            if not _vec(f, 3) or not 0.1 <= f[0] <= 3.0 or not all(0 <= v <= 1 for v in f[1:]):
                errs.append(f"{p}.friction: [slide 0.1..3, spin, roll] or one number")
        pose = o.get("pose")
        if not isinstance(pose, dict) or not (_vec(pose.get("pos"), 3) or _vec(pose.get("pos"), 2)):
            errs.append(f"{p}.pose.pos: [x, y] or [x, y, z]")
        else:
            if not _num(pose.get("yaw", 0.0)):
                errs.append(f"{p}.pose.yaw: radians")
            o["pose"] = {"pos": [float(v) for v in pose["pos"]], "yaw": float(pose.get("yaw", 0.0) or 0.0)}
            for k in sorted(set(pose) - {"pos", "yaw"}):
                errs.append(f"{p}.pose.{k}: unknown key (pose is {{pos, yaw}})")
        rz = o.get("randomize")
        if rz is not None:
            if not isinstance(rz, dict) or rz.get("region") not in regions:
                errs.append(f"{p}.randomize.region: must name a region (have: {', '.join(regions) or 'none'})")
            elif "yaw" in rz and (not _vec(rz["yaw"], 2) or rz["yaw"][0] > rz["yaw"][1]):
                errs.append(f"{p}.randomize.yaw: [lo, hi] rad")
            elif not o.get("dynamic"):
                errs.append(f"{p}.randomize: only dynamic objects are re-placed on reset")
            if isinstance(rz, dict):
                for k in sorted(set(rz) - {"region", "yaw"}):
                    errs.append(f"{p}.randomize.{k}: unknown key")
        objs[oid] = o
        if len(errs) == bad:
            good.append(o)
    spec["objects"] = [o for o in objs_raw if isinstance(o, dict)]

    # geometry only once every object reads
    if len(good) == len(objs) and objs:
        _check_geometry(spec, objs, regions, (tx0, ty0, tx1, ty1), t_top, errs, fixes)

    # cameras
    cams = spec.get("cameras")
    if cams is None:
        cams = spec["cameras"] = copy.deepcopy(DEFAULT_CAMERAS)
    if isinstance(cams, list):  # the LLM form
        cams = spec["cameras"] = {c.get("id"): {k: v for k, v in c.items() if k != "id" and v is not None}
                                  for c in cams if isinstance(c, dict)}
    if not isinstance(cams, dict):
        errs.append("$.cameras: object {name: camera}")
    else:
        for need in ("top", "wrist"):  # the camera rig (config/cameras*.yaml) and the policy expect these two
            if need not in cams:
                cams[need] = copy.deepcopy(DEFAULT_CAMERAS[need])
                fixes.append(f"cameras.{need}: added the default")
        for cn, c in cams.items():
            p = f"$.cameras.{cn}"
            if not isinstance(cn, str) or not ID_RE.match(cn):
                errs.append(f"{p}: name must be a lowercase slug")
            if not isinstance(c, dict):
                errs.append(f"{p}: object")
                continue
            if not _num(c.get("fovy", 50)) or not 20 <= c.get("fovy", 50) <= 120:
                errs.append(f"{p}.fovy: 20..120 deg")
            for k in ("width", "height"):
                if k in c and (not isinstance(c[k], int) or not 64 <= c[k] <= 1920):
                    errs.append(f"{p}.{k}: 64..1920 px")
            if cn == "wrist" or "mount" in c:
                if c.get("mount", "wrist_d405") not in WRIST_MOUNTS:
                    errs.append(f"{p}.mount: one of {', '.join(WRIST_MOUNTS)}")
                c.setdefault("mount", "wrist_d405")
            else:
                if not _vec(c.get("pos"), 3) or not _vec(c.get("target"), 3):
                    errs.append(f"{p}: a world camera needs pos [x, y, z] and target [x, y, z]")
                elif math.dist(c["pos"], c["target"]) < 0.05:
                    errs.append(f"{p}: pos and target coincide")

    # success
    succ = spec.get("success")
    if not isinstance(succ, dict):
        errs.append("$.success: {type, object, target | region} is required")
    else:
        for k in [k for k, v in succ.items() if v is None]:
            del succ[k]
        for k in sorted(set(succ) - SUCCESS_KEYS):
            errs.append(f"$.success.{k}: unknown key")
        st = succ.get("type")
        if st not in SUCCESS_TYPES:
            errs.append(f"$.success.type: one of {', '.join(SUCCESS_TYPES)}")
        so = succ.get("object")
        o = objs.get(so) if isinstance(so, str) else None
        if o is None:
            errs.append(f"$.success.object: {so!r} is not an object id")
        else:
            if not o.get("dynamic"):
                errs.append(f"$.success.object: {so} must be dynamic (the robot moves it)")
            if "graspable" not in o:
                o["graspable"] = True  # the task object (pad friction + soft contacts in MuJoCo)
            if o.get("primitive") in PRIMITIVES and all(k in o for k in (("size",) if o["primitive"] == "box" else ("radius",))):
                w = grip_width(o)
                if not GRIP_MIN_WIDTH <= w <= GRIP_MAX_WIDTH:
                    errs.append(f"$.success.object: {so} is {w * 100:.1f} cm across; the gripper needs "
                                f"{GRIP_MIN_WIDTH * 100:.1f}..{GRIP_MAX_WIDTH * 100:.1f} cm")
            if _num(o.get("mass", 0.05)) and o.get("mass", 0.05) > MAX_GRASP_MASS:
                errs.append(f"$.success.object: {so} weighs {o['mass']} kg; a friction grasp holds <= {MAX_GRASP_MASS} kg")
        tgt, reg = succ.get("target"), succ.get("region")
        if (tgt is None) == (reg is None):
            errs.append("$.success: exactly one of target (an object id) or region (a region name)")
        elif tgt is not None and tgt not in objs:
            errs.append(f"$.success.target: {tgt!r} is not an object id")
        elif tgt is not None and tgt == so:
            errs.append("$.success.target: must differ from success.object")
        elif reg is not None and reg not in regions:
            errs.append(f"$.success.region: {reg!r} is not a region")
        elif reg is not None and st == "object_on":
            errs.append("$.success: object_on needs a target object; use object_in for a region")
        if "margin" in succ and (not _num(succ["margin"]) or not 0 <= succ["margin"] <= 0.05):
            errs.append("$.success.margin: 0..0.05 m")
        if "released" in succ and not isinstance(succ["released"], bool):
            errs.append("$.success.released: bool")
        if st in SUCCESS_TYPES:
            succ.setdefault("margin", DEFAULT_MARGIN[st])
        succ.setdefault("released", True)
        # the goal footprint must be able to hold the object
        if not errs and tgt is not None:
            t = objs[tgt]
            room = inner_radius(t) - succ["margin"]
            if st == "object_in" and room < inner_radius(objs[so]) * 0.5:
                errs.append(f"$.success.target: {tgt} is too small to hold {so}")

    if errs:
        raise SceneSpecError(errs)

    # the task must not already be done at the nominal poses, nor reachable by a random spawn
    nominal = nominal_poses(spec)
    if evaluate_success(spec, nominal, held=())["success"]:
        raise SceneSpecError([f"$.success: already true at the initial poses ({describe_success(spec)})"])
    _check_spawn(spec, errs)
    if errs:
        raise SceneSpecError(errs)

    meta = spec.setdefault("meta", {})
    if not isinstance(meta, dict):
        raise SceneSpecError(["$.meta: object"])
    if fixes:
        meta["fixes"] = list(dict.fromkeys([*meta.get("fixes", []), *fixes]))
    return spec


def _check_geometry(spec: dict[str, Any], objs: dict[str, dict[str, Any]], regions: dict[str, Any],
                    table_box: tuple[float, float, float, float], t_top: float, errs: list[str], fixes: list[str]) -> None:
    tx0, ty0, tx1, ty1 = table_box
    # 1. resolve supports: an object whose centre lies over another object's footprint rests on it
    support: dict[str, str | None] = {}
    for oid, o in objs.items():
        x, y = o["pose"]["pos"][:2]
        best: tuple[float, str] | None = None
        for sid, s in objs.items():
            if sid == oid or o["primitive"] == "plate":
                continue
            sx, sy = s["pose"]["pos"][:2]
            if math.hypot(x - sx, y - sy) < inner_radius(s):
                # prefer the support that is physically lower in the stack (given z), else the larger footprint
                gz = o["pose"]["pos"][2] if len(o["pose"]["pos"]) > 2 else None
                sz = s["pose"]["pos"][2] if len(s["pose"]["pos"]) > 2 else 0.0
                if gz is not None and gz < sz:
                    continue  # it is below that one: that one is stacked on it
                key = (inner_radius(s), sid)
                best = key if best is None or key > best else best
        support[oid] = best[1] if best else None
    # 2. cycles
    for oid in objs:
        seen, cur = set(), oid
        while cur is not None:
            if cur in seen:
                errs.append(f"$.objects[{oid}]: stacking cycle ({' -> '.join(seen)})")
                return
            seen.add(cur)
            cur = support.get(cur)

    # 3. snap z bottom-up
    def rest_z(oid: str, depth: int = 0) -> float:
        o = objs[oid]
        s = support[oid]
        base = t_top if s is None else top_z(objs[s], rest_z(s, depth + 1))
        return base if o["primitive"] == "plate" else base + half_height(o)

    for oid, o in objs.items():
        want = round(rest_z(oid), 5)
        pos = o["pose"]["pos"]
        if len(pos) == 2:
            o["pose"]["pos"] = [pos[0], pos[1], want]
        elif abs(pos[2] - want) > SNAP_TOL:
            fixes.append(f"objects[{oid}].pose.z: {pos[2]:.4f} -> {want:.4f} (resting on {support[oid] or 'the table'})")
            o["pose"]["pos"] = [pos[0], pos[1], want]

    # 4. on the table, in reach, no interpenetration
    for oid, o in objs.items():
        x, y = o["pose"]["pos"][:2]
        r = footprint_radius(o)
        p = f"$.objects[{oid}]"
        if support[oid] is None and (x - r < tx0 or x + r > tx1 or y - r < ty0 or y + r > ty1):
            errs.append(f"{p}.pose: ({x:.3f}, {y:.3f}) is not fully on the table")
        d = math.hypot(x, y)
        if not REACH_MIN <= d <= REACH_MAX:
            errs.append(f"{p}.pose: {d:.3f} m from the robot base; the arm works {REACH_MIN}..{REACH_MAX} m out")
    ids = list(objs)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if support[a] == b or support[b] == a:
                continue
            if _same_level(a, b, support) and _overlap(objs[a], objs[b]):
                errs.append(f"$.objects[{a}]: overlaps {b} (centres {_dxy(objs[a], objs[b]) * 100:.1f} cm apart)")
    for rn, r in regions.items():
        if not (_vec(r.get("x"), 2) and _vec(r.get("y"), 2)):
            continue
        cx, cy = sum(r["x"]) / 2, sum(r["y"]) / 2
        if not REACH_MIN <= math.hypot(cx, cy) <= REACH_MAX:
            errs.append(f"$.regions.{rn}: centre is {math.hypot(cx, cy):.3f} m from the base (reach {REACH_MIN}..{REACH_MAX})")


def _same_level(a: str, b: str, support: dict[str, str | None]) -> bool:
    return support.get(a) == support.get(b)


def _dxy(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    return math.dist(a["pose"]["pos"][:2], b["pose"]["pos"][:2])


def _overlap(a: Mapping[str, Any], b: Mapping[str, Any], clearance: float = 0.0) -> bool:
    return _dxy(a, b) < footprint_radius(a) + footprint_radius(b) + clearance


def _check_spawn(spec: dict[str, Any], errs: list[str]) -> None:
    """Randomized objects must fit their region, keep in reach, and never spawn already in the goal."""
    objs = _objects(spec)
    succ = spec["success"]
    goal = succ.get("target")
    for oid, o in objs.items():
        rz = o.get("randomize")
        if not rz:
            continue
        r = spec["regions"][rz["region"]]
        fr = footprint_radius(o)
        if r["x"][1] - r["x"][0] < 0 or r["y"][1] - r["y"][0] < 0:
            continue
        corners = [(x, y) for x in r["x"] for y in r["y"]]
        far = max(math.hypot(x, y) for x, y in corners)
        near = min(math.hypot(x, y) for x, y in corners)
        if far > REACH_MAX + 0.02 or near < REACH_MIN - 0.02:
            errs.append(f"$.regions.{rz['region']}: spawn area for {oid} reaches {near:.2f}..{far:.2f} m; "
                        f"the arm works {REACH_MIN}..{REACH_MAX} m out")
        if oid == succ.get("object") and goal is not None and goal in objs:
            g = objs[goal]
            gx, gy = g["pose"]["pos"][:2]
            # nearest point of the (centre) spawn rectangle to the goal centre
            nx, ny = min(max(gx, r["x"][0]), r["x"][1]), min(max(gy, r["y"][0]), r["y"][1])
            if math.hypot(gx - nx, gy - ny) < footprint_radius(g) + fr:
                errs.append(f"$.objects[{oid}].randomize: its spawn region overlaps the goal {goal}")
        if oid == succ.get("object") and succ.get("region"):
            gr = spec["regions"][succ["region"]]
            if r["x"][0] < gr["x"][1] + fr and gr["x"][0] - fr < r["x"][1] and r["y"][0] < gr["y"][1] + fr \
                    and gr["y"][0] - fr < r["y"][1]:
                errs.append(f"$.objects[{oid}].randomize: its spawn region overlaps the goal region {succ['region']}")
    if any(o.get("randomize") for o in objs.values()):
        rng = random.Random(104)
        fails = sum(1 for _ in range(40) if sample_poses(spec, rng, max_tries=60) is None)
        if fails > 20:
            errs.append("$.regions: the spawn regions are too crowded to place every randomized object without overlap")


# ------------------------------------------------------------------ poses, sampling, success

def nominal_poses(spec: Mapping[str, Any]) -> dict[str, dict[str, list[float]]]:
    """{id: {pos, quat}} at the spec's poses (the scene as authored)."""
    return {o["id"]: {"pos": [float(v) for v in o["pose"]["pos"]], "quat": yaw_quat(float(o["pose"].get("yaw", 0.0)))}
            for o in spec.get("objects") or []}


def sample_poses(spec: Mapping[str, Any], rng: random.Random | None = None, *, max_tries: int = 200
                 ) -> dict[str, dict[str, list[float]]] | None:
    """Initial poses for one episode: randomized objects re-placed uniformly in their region (centre) with a
    uniform yaw, rejecting overlaps with every other object. None if no placement was found in ``max_tries``.
    Objects stacked on a randomized object move with it."""
    rng = rng or random.Random()
    objs = _objects(spec)
    poses = nominal_poses(spec)
    placed: list[str] = [oid for oid, o in objs.items() if not o.get("randomize")]
    for oid, o in objs.items():
        rz = o.get("randomize")
        if not rz:
            continue
        r = spec["regions"][rz["region"]]
        lo_y, hi_y = (rz.get("yaw") or [0.0, 0.0])
        for _ in range(max_tries):
            x, y = rng.uniform(*r["x"]), rng.uniform(*r["y"])
            cand = {**o, "pose": {"pos": [x, y, o["pose"]["pos"][2]], "yaw": 0.0}}
            if all(not _overlap(cand, {**objs[p], "pose": {"pos": poses[p]["pos"]}}, 0.005) for p in placed):
                yaw = rng.uniform(lo_y, hi_y)
                dx, dy = x - o["pose"]["pos"][0], y - o["pose"]["pos"][1]
                poses[oid] = {"pos": [x, y, o["pose"]["pos"][2]], "quat": yaw_quat(yaw)}
                for sid, s in objs.items():  # anything resting on it rides along
                    if sid != oid and not s.get("randomize") and math.hypot(
                            s["pose"]["pos"][0] - o["pose"]["pos"][0], s["pose"]["pos"][1] - o["pose"]["pos"][1]) < inner_radius(o) \
                            and s["pose"]["pos"][2] > o["pose"]["pos"][2]:
                        poses[sid] = {"pos": [s["pose"]["pos"][0] + dx, s["pose"]["pos"][1] + dy, s["pose"]["pos"][2]],
                                      "quat": poses[sid]["quat"]}
                placed.append(oid)
                break
        else:
            return None
    return poses


def evaluate_success(spec: Mapping[str, Any], poses: Mapping[str, Mapping[str, Any]], *,
                     held: Iterable[str] = ()) -> dict[str, Any]:
    """The spec's success predicate on live poses ``{id: {pos: [x,y,z], quat?}}`` (objects missing from
    ``poses`` are at their spec pose). ``held``: ids the gripper is holding (both pads in contact).

    Returns {success, predicate, object, target, over, resting, released, dxy_m, dz_m}."""
    succ = spec["success"]
    objs = _objects(spec)
    oid = succ["object"]
    o = objs[oid]

    def pos(i: str) -> list[float]:
        p = poses.get(i) if isinstance(poses, Mapping) else None
        v = (p or {}).get("pos") if isinstance(p, Mapping) else None
        return [float(x) for x in v] if v is not None else [float(x) for x in objs[i]["pose"]["pos"]]

    x, y, z = pos(oid)
    hh = half_height(o) if o["primitive"] != "plate" else 0.0
    margin = float(succ.get("margin", DEFAULT_MARGIN.get(succ["type"], 0.0)))
    if succ.get("region"):
        r = spec["regions"][succ["region"]]
        over = r["x"][0] + margin <= x <= r["x"][1] - margin and r["y"][0] + margin <= y <= r["y"][1] - margin
        cx, cy = sum(r["x"]) / 2, sum(r["y"]) / 2
        dxy = math.hypot(x - cx, y - cy)
        surface = float(spec["table"]["top_z"])
        target = succ["region"]
    else:
        target = succ["target"]
        t = objs[target]
        tx, ty, tz = pos(target)
        dxy = math.hypot(x - tx, y - ty)
        over = dxy < max(inner_radius(t) - margin, 0.0)
        surface = top_z(t, tz)
    dz = z - (surface + hh)
    resting = abs(dz) <= REST_TOL
    released = (oid not in set(held)) if succ.get("released", True) else True
    return {"success": bool(over and resting and released), "predicate": describe_success(spec), "object": oid,
            "target": target, "over": bool(over), "resting": bool(resting), "released": bool(released),
            "dxy_m": round(dxy, 4), "dz_m": round(dz, 4)}


def describe_success(spec: Mapping[str, Any]) -> str:
    """The spoken form: ``inside(cube, plate)`` / ``on_top(red_block, blue_block)``."""
    s = spec.get("success") or {}
    fn = "on_top" if s.get("type") == "object_on" else "inside"
    return f"{fn}({s.get('object')}, {s.get('target') or s.get('region')})"


def summary(spec: Mapping[str, Any]) -> dict[str, Any]:
    """A small card for lists and event payloads."""
    return {"name": spec.get("name"), "task": spec.get("task"), "objects": [o.get("id") for o in spec.get("objects") or []],
            "success": describe_success(spec)}


# ------------------------------------------------------------------ LLM authoring form

# Strict-structured-output schema (every key required; "not applicable" = null). ``from_llm`` turns an
# answer into the spec shape above (lists -> dicts, hex colours -> RGBA); ``validate`` then checks it.
_NUM = {"type": "number"}
_NNUM = {"type": ["number", "null"]}
_XY = {"type": "array", "items": _NUM, "description": "[lo, hi] in metres"}
LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "task", "notes", "table_color", "objects", "regions", "success"],
    "properties": {
        "name": {"type": "string", "description": "lowercase slug, e.g. 'cube_plate'"},
        "task": {"type": "string", "description": "the task sentence"},
        "notes": {"type": "string", "description": "one or two sentences: what is in the scene and why"},
        "table_color": {"type": ["string", "null"], "description": "'#rrggbb' or null for the default wood"},
        "objects": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "label", "primitive", "size", "radius", "height", "color", "rim_color", "mass",
                         "dynamic", "x", "y", "z", "yaw", "spawn_region", "spawn_yaw"],
            "properties": {
                "id": {"type": "string", "description": "lowercase slug, e.g. 'red_block'"},
                "label": {"type": "string", "description": "what a person calls it, e.g. 'the red block'"},
                "primitive": {"type": "string", "enum": list(PRIMITIVES)},
                "size": {"type": ["array", "null"], "items": _NUM, "description": "box only: [sx, sy, sz] FULL extents, m"},
                "radius": _NNUM, "height": {**_NNUM, "description": "cylinder / plate: FULL height, m"},
                "color": {"type": "string", "description": "'#rrggbb'"},
                "rim_color": {"type": ["string", "null"], "description": "plate only: rim '#rrggbb'"},
                "mass": {**_NNUM, "description": "kg, dynamic objects only"},
                "dynamic": {"type": "boolean", "description": "true = loose object the robot can push or pick"},
                "x": _NUM, "y": _NUM,
                "z": {**_NNUM, "description": "null = resting on what is under it (computed)"},
                "yaw": _NUM,
                "spawn_region": {"type": ["string", "null"], "description": "region name it is re-placed in on every reset"},
                "spawn_yaw": {"type": ["array", "null"], "items": _NUM, "description": "[lo, hi] rad"},
            }}},
        "regions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "x", "y", "visible", "color"],
            "properties": {"name": {"type": "string"}, "x": _XY, "y": _XY,
                           "visible": {"type": "boolean", "description": "true = drawn on the table (a goal zone)"},
                           "color": {"type": ["string", "null"]}}}},
        "success": {"type": "object", "additionalProperties": False,
                    "required": ["type", "object", "target", "region"],
                    "properties": {"type": {"type": "string", "enum": list(SUCCESS_TYPES)},
                                   "object": {"type": "string"},
                                   "target": {"type": ["string", "null"], "description": "an object id, or null"},
                                   "region": {"type": ["string", "null"], "description": "a region name, or null"}}},
    },
}


def from_llm(ans: Mapping[str, Any]) -> dict[str, Any]:
    """The LLM authoring form (``LLM_SCHEMA``) -> a raw spec for ``validate``."""
    objects = []
    for o in ans.get("objects") or []:
        d: dict[str, Any] = {"id": o.get("id"), "label": o.get("label"), "primitive": o.get("primitive"),
                             "color": o.get("color"), "dynamic": bool(o.get("dynamic"))}
        if o.get("primitive") == "box":
            d["size"] = o.get("size")
        else:
            d["radius"] = o.get("radius")
            if o.get("primitive") in ("cylinder", "plate"):
                d["height"] = o.get("height")
        if o.get("primitive") == "plate" and o.get("rim_color"):
            d["rim_color"] = o["rim_color"]
        if d["dynamic"] and o.get("mass") is not None:
            d["mass"] = o["mass"]
        pos = [o.get("x"), o.get("y")] + ([o["z"]] if o.get("z") is not None else [])
        d["pose"] = {"pos": pos, "yaw": o.get("yaw") or 0.0}
        if o.get("spawn_region"):
            d["randomize"] = {"region": o["spawn_region"], **({"yaw": o["spawn_yaw"]} if o.get("spawn_yaw") else {})}
        objects.append({k: v for k, v in d.items() if v is not None})
    regions = {}
    for r in ans.get("regions") or []:
        e: dict[str, Any] = {"x": r.get("x"), "y": r.get("y")}
        if r.get("visible"):
            e["visible"] = True
        if r.get("color"):
            e["color"] = r["color"]
        regions[r.get("name")] = e
    s = ans.get("success") or {}
    succ = {"type": s.get("type"), "object": s.get("object")}
    if s.get("target"):
        succ["target"] = s["target"]
    if s.get("region"):
        succ["region"] = s["region"]
    out: dict[str, Any] = {"version": SCHEMA_VERSION, "name": ans.get("name") or None, "task": ans.get("task"),
                           "notes": ans.get("notes") or "", "objects": objects, "regions": regions, "success": succ}
    if out["name"] is None or not ID_RE.match(str(out["name"])):
        out["name"] = slugify(str(ans.get("name") or ans.get("task") or ""))
    if ans.get("table_color"):
        out["table"] = {**copy.deepcopy(DEFAULT_TABLE), "color": ans["table_color"]}
    return out


# The default sim scene (= services/robot-client/.../sim_assets/scenes/cube_plate.json), also the
# generator's worked example.
CUBE_PLATE: dict[str, Any] = {
    "version": 1, "name": "cube_plate", "task": "Pick up the cube and place it in the empty plate.",
    "notes": "A red cube, the EMPTY target plate (blue rim) and a second plate that already holds a yellow block, "
             "so 'the empty plate' means something.",
    "physics": {"timestep": 0.002},
    "table": copy.deepcopy(DEFAULT_TABLE),
    "objects": [
        {"id": "cube", "label": "the red cube", "primitive": "box", "size": [0.04, 0.04, 0.04],
         "color": [0.86, 0.12, 0.10, 1.0], "mass": 0.05, "friction": [1.6, 0.02, 0.002], "dynamic": True,
         "graspable": True, "pose": {"pos": [0.31, -0.14, 0.02], "yaw": 0.0},
         "randomize": {"region": "cube_spawn", "yaw": [-0.7, 0.7]}},
        {"id": "plate", "label": "the empty plate", "primitive": "plate", "radius": 0.075, "height": 0.012,
         "color": [0.94, 0.94, 0.92, 1.0], "rim_color": [0.20, 0.42, 0.80, 1.0], "dynamic": False,
         "pose": {"pos": [0.31, 0.19, 0.0], "yaw": 0.0}},
        {"id": "full_plate", "label": "the plate with the yellow block", "primitive": "plate", "radius": 0.075,
         "height": 0.012, "color": [0.94, 0.94, 0.92, 1.0], "rim_color": [0.75, 0.75, 0.78, 1.0], "dynamic": False,
         "pose": {"pos": [0.47, -0.05, 0.0], "yaw": 0.0}},
        {"id": "filler", "label": "the yellow block", "primitive": "cylinder", "radius": 0.028, "height": 0.05,
         "color": [0.95, 0.78, 0.15, 1.0], "mass": 0.08, "dynamic": True, "pose": {"pos": [0.47, -0.05, 0.0371], "yaw": 0.0}},
    ],
    "regions": {"cube_spawn": {"x": [0.25, 0.37], "y": [-0.21, -0.07]}},
    "cameras": copy.deepcopy(DEFAULT_CAMERAS),
    "success": {"type": "object_in", "object": "cube", "target": "plate", "margin": 0.012, "released": True},
}


def default_spec() -> dict[str, Any]:
    return validate(CUBE_PLATE)


__all__ = ["CUBE_PLATE", "DEFAULT_CAMERAS", "DEFAULT_TABLE", "GRIP_MAX_WIDTH", "LLM_SCHEMA", "PRIMITIVES", "REACH_MAX",
           "REACH_MIN", "SCHEMA_VERSION", "SUCCESS_TYPES", "SceneSpecError", "color_hex", "color_rgba", "default_spec",
           "describe_success", "evaluate_success", "from_llm", "half_height", "inner_radius", "nominal_poses",
           "sample_poses", "slugify", "summary", "top_z", "validate", "yaw_quat"]
