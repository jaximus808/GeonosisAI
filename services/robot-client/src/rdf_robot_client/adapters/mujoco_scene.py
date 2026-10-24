"""MuJoCo scenes for the [SIM] YAM, built from a SCENE SPEC (no hard-coded objects).

``load_scene(spec)`` takes a scene spec (a dict, a path to a JSON file, or the name of a file in
``sim_assets/scenes/``, default ``cube_plate``) and returns a ``Scene``; ``build_scene_xml(scene)``
turns it into the MJCF. Shared by the MuJoCo adapter (``mujoco_sim.py``) and the scripted sim
policy (``sim_policy.py``). TDM-104 owns the canonical schema (``rdf_common.scene_spec``); when that
module exists and exposes ``validate(spec) -> dict``, ``load_scene`` runs it first.

Spec shape (see ``sim_assets/scenes/cube_plate.json``)::

  {"version": 1, "name", "task",
   "physics": {"timestep": 0.002},
   "table": {"center": [x, y], "size": [sx, sy], "top_z": 0.0, "color": [r, g, b, a]},
   "objects": [{"id", "primitive": box|cylinder|sphere|plate, "size": [sx, sy, sz] (box, full extents) |
                "radius" + "height" (cylinder / plate, full height) | "radius" (sphere),
                "color", "rim_color"? (plate), "mass"?, "friction"?, "dynamic": bool, "graspable"?,
                "pose": {"pos": [x, y, z], "yaw": rad},
                "randomize"?: {"region": <name>, "yaw": [lo, hi]}}],
   "regions": {<name>: {"x": [lo, hi], "y": [lo, hi]}},
   "cameras": {"top": {"pos", "target", "fovy", "width", "height"}, "wrist": {"mount": "wrist_d405", "fovy", ...}},
   "success": {"type": "object_in", "object": <id>, "target": <id> | "region": <name>, "margin": m, "released": bool}}

The arm is the SDK's own model, vendored unchanged from i2rt-robotics/i2rt @ 120c3c8 (MIT, see
``sim_assets/i2rt_yam/README.md``): ``yam_linear_4310.xml`` is exactly what the SDK's
``combine_arm_and_gripper_xml(ArmType.YAM, GripperType.LINEAR_4310)`` writes, with mesh paths made
relative. This module adds only what the SDK model leaves out on purpose (it is kinematics-only):

  * actuators: joint-space position servos with the YAM v1 PD gains (kp 80/80/80/10/10/10,
    kd 5/5/5/1.5/1.5/1.5, +-10 Nm, ``yam-sdk-notes.md`` section 2) plus ``gravcomp`` on every arm
    body, which is what the real arm's MIT-mode PD + MuJoCo gravity feed-forward amounts to;
    a position servo on the finger slide (``joint7``; ``joint8`` follows by the SDK's equality);
  * contacts: only the two finger pads collide (with the objects and the table); the arm links
    are visual-only so the convex hulls of neighbouring links can't fight each other;
  * a wrist camera at the D405 mount pose of the SDK's ``yam_station_linear_4310_d405`` model
    (flange -> camera xyz (0, -0.0704, 0.0770), quat (-0.976, 0.216, 0, 0)), with its mesh.

Frames: world == robot base frame (x forward, z up, metres); the table top is ``table.top_z`` (0).
Gripper value g: 1 = open (finger slides at 0.0475 m, 9.4 cm between the pads), 0 = closed (slides
at 0, pads touching). The fingers close along the tool (grasp_site) y axis; tool z is the approach.

Finger contacts: the SDK's tip meshes are L-shaped, so their convex hulls (what MuJoCo collides)
overlap each other and the object long before the real pads touch. The meshes stay visual and each
tip gets an invisible 3.6 x 1.0 x 3.5 cm box at its distal pad, measured from the tip mesh vertices
(``spikes/mujoco-sim``): pad faces at tool y = +-0.0005 (closed) .. +-0.047 (open), z in [-0.035, 0].
"""
from __future__ import annotations

import copy
import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ASSET_DIR = Path(__file__).resolve().parents[1] / "sim_assets" / "i2rt_yam"
SCENES_DIR = Path(__file__).resolve().parents[1] / "sim_assets" / "scenes"
ARM_XML = ASSET_DIR / "yam_linear_4310.xml"
SDK_REF = "i2rt-robotics/i2rt@120c3c8"
DEFAULT_SCENE = "cube_plate"

ARM_JOINTS = ("joint1", "joint2", "joint3", "joint4", "joint5", "joint6")
FINGER_JOINT = "joint7"
FINGER_TRAVEL = 0.0475  # m, SDK model range of joint7 / joint8
KP = (80.0, 80.0, 80.0, 10.0, 10.0, 10.0)  # YAM v1 MIT-mode gains (yam-sdk-notes.md section 2)
KD = (5.0, 5.0, 5.0, 1.5, 1.5, 1.5)
TORQUE_LIMIT = 10.0
PRIMITIVES = ("box", "cylinder", "sphere", "plate")
DEFAULT_FRICTION = (1.0, 0.01, 0.001)
PAD_FRICTION = (1.6, 0.02, 0.002)

# finger pad boxes in each tip body's frame (computed from the SDK tip meshes at slide 0, see docstring)
_PADS = {
    "tip_left": ("-0.07259 0.02399 -0.04952", "-0.5 0.5 0.5 -0.5"),
    "tip_right": ("-0.0238 -0.07259 -0.0495", "0.70711 0.70711 0 0"),
}
# D405 wrist mount (flange -> optical frame) from the SDK's yam_station_linear_4310_d405 model
_WRIST_MOUNTS = {"wrist_d405": ("-7.6568e-08 -0.070435 0.077006", "-0.976295 0.216445 1.79307e-06 3.97516e-07")}


class SceneError(ValueError):
    """The scene spec is malformed."""


@dataclass
class Scene:
    """A loaded scene spec plus the derived numbers the adapter and the scripted policy need."""

    spec: dict[str, Any]
    name: str = DEFAULT_SCENE
    task: str = ""
    timestep: float = 0.002
    table_top: float = 0.0
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    regions: dict[str, dict[str, Any]] = field(default_factory=dict)
    cameras: dict[str, dict[str, Any]] = field(default_factory=dict)
    success: dict[str, Any] = field(default_factory=dict)

    # ---- object geometry
    def half_height(self, oid: str) -> float:
        o = self.objects[oid]
        p = o["primitive"]
        if p == "box":
            return float(o["size"][2]) / 2
        if p == "sphere":
            return float(o["radius"])
        return float(o["height"]) / 2

    def footprint_radius(self, oid: str) -> float:
        o = self.objects[oid]
        if o["primitive"] == "box":
            return math.hypot(float(o["size"][0]), float(o["size"][1])) / 2
        return float(o["radius"])

    def resting_z(self, oid: str) -> float:
        return self.table_top + self.half_height(oid)

    def top_z(self, oid: str) -> float:
        """Height of the object's upper surface at its spec pose (plates: the plate top)."""
        o = self.objects[oid]
        z = float(o["pose"]["pos"][2]) if len(o["pose"]["pos"]) > 2 else self.table_top
        if o["primitive"] == "plate":
            return z + float(o["height"])
        return z + self.half_height(oid)

    # ---- the task
    @property
    def target(self) -> str | None:
        """The object the task moves (success.object)."""
        return self.success.get("object")

    @property
    def container(self) -> str | None:
        """Where it goes (success.target), if that is an object."""
        t = self.success.get("target")
        return t if t in self.objects else None

    def place_xy(self) -> tuple[float, float]:
        c = self.container
        if c is not None:
            p = self.objects[c]["pose"]["pos"]
            return float(p[0]), float(p[1])
        r = self.regions[self.success["region"]]
        return (sum(r["x"]) / 2, sum(r["y"]) / 2)

    def place_radius(self) -> float:
        c = self.container
        if c is not None:
            return self.footprint_radius(c)
        r = self.regions[self.success["region"]]
        return min(r["x"][1] - r["x"][0], r["y"][1] - r["y"][0]) / 2

    def place_top_z(self) -> float:
        c = self.container
        return self.top_z(c) if c is not None else self.table_top

    def dynamic_objects(self) -> list[str]:
        return [oid for oid, o in self.objects.items() if o.get("dynamic")]

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "task": self.task, "objects": list(self.objects), "target": self.target,
                "container": self.container or self.success.get("region")}


def _resolve(spec: dict[str, Any] | str | Path | None) -> dict[str, Any]:
    if spec is None or spec == "":
        spec = DEFAULT_SCENE
    if isinstance(spec, dict):
        return copy.deepcopy(spec)
    p = Path(spec)
    if not p.suffix and not p.exists():
        p = SCENES_DIR / f"{spec}.json"
    if not p.exists():
        known = sorted(x.stem for x in SCENES_DIR.glob("*.json"))
        raise SceneError(f"scene {spec!r} not found (a JSON path, or one of {known})")
    return json.loads(p.read_text())


def load_scene(spec: dict[str, Any] | str | Path | None = None) -> Scene:
    """Load and check a scene spec (dict, JSON path, or scene name under ``sim_assets/scenes``)."""
    raw = _resolve(spec)
    try:  # TDM-104's canonical validator, when it lands
        from rdf_common import scene_spec as _canon  # type: ignore[attr-defined]

        if callable(getattr(_canon, "validate", None)):
            out = _canon.validate(raw)
            raw = out if isinstance(out, dict) else getattr(out, "model_dump", lambda: raw)()
    except ImportError:
        pass
    objs: dict[str, dict[str, Any]] = {}
    for o in raw.get("objects") or []:
        oid = str(o.get("id") or "")
        if not oid or oid in objs:
            raise SceneError(f"every object needs a unique id (got {oid!r})")
        if o.get("primitive") not in PRIMITIVES:
            raise SceneError(f"object {oid}: primitive must be one of {PRIMITIVES}")
        if o["primitive"] == "box" and len(o.get("size") or []) != 3:
            raise SceneError(f"object {oid}: a box needs size [sx, sy, sz]")
        if o["primitive"] in ("cylinder", "plate") and not (o.get("radius") and o.get("height")):
            raise SceneError(f"object {oid}: a {o['primitive']} needs radius and height")
        if o["primitive"] == "sphere" and not o.get("radius"):
            raise SceneError(f"object {oid}: a sphere needs radius")
        o = {**o, "pose": {"pos": list((o.get("pose") or {}).get("pos") or [0.3, 0.0]),
                           "yaw": float((o.get("pose") or {}).get("yaw") or 0.0)}}
        objs[oid] = o
    regions = dict(raw.get("regions") or {})
    for oid, o in objs.items():
        rz = o.get("randomize")
        if rz and rz.get("region") not in regions:
            raise SceneError(f"object {oid}: randomize.region {rz.get('region')!r} is not in regions")
    succ = dict(raw.get("success") or {})
    if succ:
        if succ.get("object") not in objs:
            raise SceneError(f"success.object {succ.get('object')!r} is not an object")
        if succ.get("target") is None and succ.get("region") not in regions:
            raise SceneError("success needs target (an object id) or region (a region name)")
        if succ.get("target") is not None and succ.get("target") not in objs:
            raise SceneError(f"success.target {succ.get('target')!r} is not an object")
    table = raw.get("table") or {}
    scene = Scene(spec=raw, name=str(raw.get("name") or "scene"), task=str(raw.get("task") or ""),
                  timestep=float((raw.get("physics") or {}).get("timestep") or 0.002),
                  table_top=float(table.get("top_z") or 0.0), objects=objs, regions=regions,
                  cameras=dict(raw.get("cameras") or {}), success=succ)
    for oid, o in objs.items():  # z defaults to resting on the table
        if len(o["pose"]["pos"]) < 3:
            o["pose"]["pos"] = [*o["pose"]["pos"][:2], 0.0 if o["primitive"] == "plate" else scene.resting_z(oid)]
    return scene


def _f(v) -> str:
    return " ".join(f"{float(x):.6g}" for x in v)


def _look_at_xyaxes(pos, target) -> str:
    """MuJoCo camera xyaxes for a camera at ``pos`` looking at ``target`` with world +z up."""
    fx, fy, fz = (target[i] - pos[i] for i in range(3))
    n = math.sqrt(fx * fx + fy * fy + fz * fz)
    fwd = (fx / n, fy / n, fz / n)
    up = (0.0, 0.0, 1.0)
    # x = fwd x up (camera right), y = x x fwd (camera up); MuJoCo cameras look along -z
    x = (fwd[1] * up[2] - fwd[2] * up[1], fwd[2] * up[0] - fwd[0] * up[2], fwd[0] * up[1] - fwd[1] * up[0])
    nx = math.sqrt(sum(c * c for c in x))
    x = tuple(c / nx for c in x)
    y = (x[1] * fwd[2] - x[2] * fwd[1], x[2] * fwd[0] - x[0] * fwd[2], x[0] * fwd[1] - x[1] * fwd[0])
    return _f((*x, *y))


def _quat_yaw(yaw: float) -> str:
    return _f((math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)))


def _add_object(world: ET.Element, scene: Scene, oid: str) -> None:
    o = scene.objects[oid]
    p = o["primitive"]
    pos, yaw = o["pose"]["pos"], o["pose"]["yaw"]
    rgba = _f(o.get("color") or (0.7, 0.7, 0.7, 1.0))
    fric = _f(o.get("friction") or (PAD_FRICTION if o.get("graspable") else DEFAULT_FRICTION))
    body = ET.SubElement(world, "body", {"name": oid, "pos": _f(pos), "quat": _quat_yaw(yaw)})
    if o.get("dynamic"):
        ET.SubElement(body, "freejoint", {"name": f"{oid}_free"})
    common = {"rgba": rgba, "friction": fric, "contype": "1", "conaffinity": "1"}
    if o.get("dynamic"):
        common["mass"] = f"{float(o.get('mass') or 0.05):g}"
    if o.get("graspable"):
        common.update(condim="4", solref="0.004 1", solimp="0.95 0.99 0.001")
    if p == "box":
        ET.SubElement(body, "geom", {"name": oid, "type": "box", "size": _f(v / 2 for v in o["size"]), **common})
    elif p == "cylinder":
        ET.SubElement(body, "geom", {"name": oid, "type": "cylinder",
                                     "size": _f((o["radius"], o["height"] / 2)), **common})
    elif p == "sphere":
        ET.SubElement(body, "geom", {"name": oid, "type": "sphere", "size": _f((o["radius"],)), **common})
    else:  # plate: a thin disc + a ring of boxes, so an object visibly sits IN it
        r, h = float(o["radius"]), float(o["height"])
        ET.SubElement(body, "geom", {"name": oid, "type": "cylinder", "pos": _f((0, 0, h / 2)),
                                     "size": _f((r, h / 2)), **common})
        n_rim = 16
        rim = _f(o.get("rim_color") or o.get("color") or (0.7, 0.7, 0.7, 1))
        for k in range(n_rim):
            a = 2 * math.pi * k / n_rim
            ET.SubElement(body, "geom", {
                "name": f"{oid}_rim{k}", "type": "box", "pos": _f((r * math.cos(a), r * math.sin(a), h + 0.004)),
                "euler": _f((0, 0, a)), "size": _f((0.004, r * math.pi / n_rim + 0.002, 0.005)),
                "rgba": rim, "contype": "1", "conaffinity": "1"})


def build_scene_xml(scene: Scene | dict[str, Any] | str | None = None) -> str:
    """The full MJCF for ``scene`` (mesh paths resolve against ``ASSET_DIR``)."""
    s = scene if isinstance(scene, Scene) else load_scene(scene)
    tree = ET.parse(ARM_XML)
    root = tree.getroot()
    root.set("model", f"rdf_sim_yam_{s.name}")
    comp = root.find("compiler")
    comp.set("meshdir", str(ASSET_DIR))
    comp.set("autolimits", "true")

    world = root.find("worldbody")
    arm_base = world.find("body[@name='base']")
    # contacts: finger pads only; gravity compensation on every arm body (the real arm's feed-forward)
    for body in arm_base.iter("body"):
        body.set("gravcomp", "1")
        for g in body.findall("geom") + [g for fr in body.findall("frame") for g in fr.findall("geom")]:
            g.set("contype", "0")
            g.set("conaffinity", "0")
    for g in arm_base.findall("geom"):  # the base itself
        g.set("contype", "0")
        g.set("conaffinity", "0")
    for tip in arm_base.iter("body"):
        pad = _PADS.get(tip.get("name") or "")
        if pad:
            ET.SubElement(tip, "geom", {"name": f"{tip.get('name')}_pad", "type": "box", "pos": pad[0], "quat": pad[1],
                                        "size": "0.018 0.005 0.0175", "rgba": "0 0 0 0", "contype": "1",
                                        "conaffinity": "1", "friction": _f(PAD_FRICTION), "condim": "4",
                                        "solref": "0.004 1", "solimp": "0.95 0.99 0.001", "mass": "0"})
    for j in arm_base.iter("joint"):
        if j.get("name") in ARM_JOINTS:
            j.set("damping", "0.05")
            j.set("armature", "0.01")
        elif j.get("name") in ("joint7", "joint8"):
            j.set("damping", "2")
            j.set("armature", "0.001")

    asset = root.find("asset")
    # wrist camera (scene spec cameras.wrist.mount; the D405 pose is the only mount the SDK models)
    wrist = s.cameras.get("wrist")
    if wrist is not None:
        mount = _WRIST_MOUNTS.get(str(wrist.get("mount") or "wrist_d405"))
        if mount is None:
            raise SceneError(f"cameras.wrist.mount must be one of {sorted(_WRIST_MOUNTS)}")
        flange = arm_base.find(".//body[@name='gripper']")
        cam_body = ET.SubElement(flange, "body", {"name": "wrist_cam_mount", "pos": mount[0], "quat": mount[1],
                                                  "gravcomp": "1"})
        ET.SubElement(cam_body, "inertial", {"pos": "0 0 0", "mass": "1e-4", "diaginertia": "1e-8 1e-8 1e-8"})
        ET.SubElement(cam_body, "geom", {"type": "mesh", "mesh": "d405_wrist", "rgba": "0.615686 0.811765 0.929412 1",
                                         "contype": "0", "conaffinity": "0"})
        # optical frame +z forward / +y down -> MuJoCo camera looks along -z with +y up: rotate pi about x
        ET.SubElement(cam_body, "camera", {"name": "wrist", "pos": "0 0 0", "quat": "0 1 0 0",
                                           "fovy": f"{float(wrist.get('fovy') or 72):g}"})
        ET.SubElement(asset, "mesh", {"name": "d405_wrist", "file": "assets/station/d405_wrist_linear_4310.stl"})
    ET.SubElement(asset, "texture", {"name": "sky", "type": "skybox", "builtin": "gradient", "rgb1": "0.42 0.52 0.66",
                                     "rgb2": "0.08 0.10 0.14", "width": "256", "height": "256"})
    ET.SubElement(asset, "texture", {"name": "floor", "type": "2d", "builtin": "checker", "rgb1": "0.22 0.23 0.25",
                                     "rgb2": "0.17 0.18 0.20", "width": "512", "height": "512"})
    ET.SubElement(asset, "material", {"name": "floor", "texture": "floor", "texrepeat": "8 8", "reflectance": "0.05"})
    tcol = (s.spec.get("table") or {}).get("color") or (0.62, 0.50, 0.37, 1.0)
    ET.SubElement(asset, "texture", {"name": "wood", "type": "2d", "builtin": "flat", "rgb1": _f(tcol[:3]),
                                     "rgb2": _f(c * 0.9 for c in tcol[:3]), "mark": "random", "random": "0.25",
                                     "markrgb": _f(c * 0.82 for c in tcol[:3]), "width": "512", "height": "512"})
    ET.SubElement(asset, "material", {"name": "table", "texture": "wood", "texrepeat": "3 3", "specular": "0.15",
                                      "shininess": "0.2"})

    # ---- table, lights, floor
    t = s.spec.get("table") or {}
    cx, cy = (t.get("center") or (0.30, 0.0))[:2]
    sx, sy = (t.get("size") or (0.96, 0.96))[:2]
    top = s.table_top
    ET.SubElement(world, "light", {"name": "key", "pos": "0.6 -0.6 1.6", "dir": "-0.3 0.35 -1", "diffuse": "0.75 0.75 0.72",
                                   "specular": "0.2 0.2 0.2", "castshadow": "true"})
    ET.SubElement(world, "light", {"name": "fill", "pos": "-0.4 0.6 1.2", "dir": "0.4 -0.4 -1", "diffuse": "0.35 0.36 0.4",
                                   "castshadow": "false"})
    ET.SubElement(world, "geom", {"name": "floor", "type": "plane", "pos": _f((0, 0, top - 0.74)), "size": "3 3 0.1",
                                  "material": "floor", "contype": "1", "conaffinity": "1"})
    ET.SubElement(world, "geom", {"name": "table", "type": "box", "pos": _f((cx, cy, top - 0.02)),
                                  "size": _f((sx / 2, sy / 2, 0.02)), "material": "table", "contype": "1",
                                  "conaffinity": "1", "friction": "0.8 0.01 0.001"})
    for i, (lx, ly) in enumerate(((-1, -1), (-1, 1), (1, -1), (1, 1))):
        ET.SubElement(world, "geom", {"name": f"table_leg{i}", "type": "box",
                                      "pos": _f((cx + lx * (sx / 2 - 0.04), cy + ly * (sy / 2 - 0.04), top - 0.39)),
                                      "size": "0.025 0.025 0.35", "rgba": "0.3 0.3 0.32 1", "contype": "0",
                                      "conaffinity": "0"})
    for oid in s.objects:
        _add_object(world, s, oid)
    for name, cam in s.cameras.items():
        if name == "wrist" or cam.get("mount"):
            continue
        ET.SubElement(world, "camera", {"name": name, "pos": _f(cam["pos"]),
                                        "xyaxes": _look_at_xyaxes(cam["pos"], cam.get("target") or (0.3, 0, 0)),
                                        "fovy": f"{float(cam.get('fovy') or 50):g}"})

    # ---- actuators
    act = ET.SubElement(root, "actuator")
    for j, kp, kd in zip(ARM_JOINTS, KP, KD):
        ET.SubElement(act, "position", {"name": f"act_{j}", "joint": j, "kp": f"{kp:g}", "kv": f"{kd:g}",
                                        "forcerange": f"{-TORQUE_LIMIT:g} {TORQUE_LIMIT:g}"})
    ET.SubElement(act, "position", {"name": "act_gripper", "joint": FINGER_JOINT, "kp": "400", "kv": "8",
                                    "ctrlrange": f"0 {FINGER_TRAVEL:g}", "forcerange": "-25 25"})

    # ---- simulation options and offscreen buffer
    opt = ET.Element("option", {"timestep": f"{s.timestep:g}", "integrator": "implicitfast", "cone": "elliptic",
                                "impratio": "10", "noslip_iterations": "3"})
    root.insert(1, opt)
    vis = ET.Element("visual")
    ET.SubElement(vis, "global", {"offwidth": "1280", "offheight": "960"})
    ET.SubElement(vis, "quality", {"shadowsize": "2048", "offsamples": "4"})
    ET.SubElement(vis, "headlight", {"ambient": "0.25 0.25 0.25", "diffuse": "0.35 0.35 0.35"})
    root.insert(2, vis)
    return ET.tostring(root, encoding="unicode")


__all__ = ["ARM_JOINTS", "ARM_XML", "ASSET_DIR", "DEFAULT_SCENE", "FINGER_JOINT", "FINGER_TRAVEL", "KD", "KP",
           "PRIMITIVES", "SCENES_DIR", "SDK_REF", "Scene", "SceneError", "build_scene_xml", "load_scene"]
