"""TDM-97: build the web twin's YAM assets from the I2RT SDK @ 120c3c8.

  uv run --no-project --with mujoco==3.3.7 --with numpy --with pyyaml --with trimesh \
      --with fast-simplification python -I spikes/tdm-97/build_yam_assets.py <i2rt checkout>

What it does
  1. Builds the arm + linear_4310 gripper MJCF with the SDK's own
     ``combine_arm_and_gripper_xml(ArmType.YAM, GripperType.LINEAR_4310)`` (the same model
     ``yam_kinematics`` and the MuJoCo sim use) and compiles it with MuJoCo.
  2. Exports the kinematic tree (body pos/quat relative to the parent, joint axes and ranges,
     the grasp_site) to ``apps/web/src/components/robotview/yamModel.ts``.
  3. Exports every visual mesh geom, with its geom pose baked into the body frame, decimated for
     the web, to ``apps/web/public/robot/yam/yam.glb`` (one node per geom, named
     ``<body>__<mesh>``), and copies the SDK's MIT LICENSE next to it.

The meshes are MuJoCo's compiled mesh vertices (already re-centred; geom_pos/geom_quat put them
back), so the twin is exactly what MuJoCo renders, minus the decimation.
"""
from __future__ import annotations

import json
import shutil
import sys
import types
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
OUT_GLB_DIR = REPO / "apps/web/public/robot/yam"
OUT_TS = REPO / "apps/web/src/components/robotview/yamModel.ts"
SDK_REF = "i2rt-robotics/i2rt@120c3c8"
# Target triangle budget per mesh (fraction of the original, with a floor).
KEEP = 0.35
MIN_TRIS = 1500


def combined_xml(sdk: Path) -> str:
    sys.path.insert(0, str(sdk))
    # utils.py imports the CAN motor driver at module level (python-can); the XML combine does not
    # use it, so stub it rather than install the hardware stack.
    stub = types.ModuleType("i2rt.motor_drivers.dm_driver")
    stub.DMChainCanInterface = object
    sys.modules["i2rt.motor_drivers.dm_driver"] = stub
    from i2rt.robots.utils import ArmType, GripperType, combine_arm_and_gripper_xml  # noqa: E402

    out = combine_arm_and_gripper_xml(ArmType.YAM, GripperType.LINEAR_4310)
    p = Path(out)
    return p.read_text() if p.exists() else out


# D405 wrist camera on the flange, at the pose of the SDK's yam_station_linear_4310_d405 model (the
# same mount and camera frame the MuJoCo sim's "wrist" camera uses, mujoco_scene.py).
WRIST_MOUNT_POS = "-7.6568e-08 -0.070435 0.077006"
WRIST_MOUNT_QUAT = "-0.976295 0.216445 1.79307e-06 3.97516e-07"
WRIST_FOVY = 72.0


def with_wrist_camera(xml: str, sdk: Path) -> str:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml)
    ET.SubElement(root.find("asset"), "mesh", {
        "name": "d405_wrist", "file": str(sdk / "i2rt/robot_models/station/assets/d405_wrist_linear_4310.stl")})
    flange = root.find(".//body[@name='gripper']")
    mount = ET.SubElement(flange, "body", {"name": "wrist_cam_mount", "pos": WRIST_MOUNT_POS, "quat": WRIST_MOUNT_QUAT})
    ET.SubElement(mount, "geom", {"type": "mesh", "mesh": "d405_wrist", "contype": "0", "conaffinity": "0"})
    # optical frame +z forward / +y down -> camera looks along -z with +y up: rotate pi about x
    ET.SubElement(mount, "site", {"name": "wrist_cam", "pos": "0 0 0", "quat": "0 1 0 0"})
    return ET.tostring(root, encoding="unicode")


def quat_to_mat(q):
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def main() -> None:
    import mujoco
    import trimesh
    import fast_simplification

    sdk = Path(sys.argv[1]).resolve()
    xml = with_wrist_camera(combined_xml(sdk), sdk)
    model = mujoco.MjModel.from_xml_string(xml)
    name = lambda kind, i: mujoco.mj_id2name(model, kind, i)  # noqa: E731

    bodies = []
    for b in range(1, model.nbody):
        jadr, jnum = int(model.body_jntadr[b]), int(model.body_jntnum[b])
        joint = None
        if jnum == 1:
            j = jadr
            assert np.allclose(model.jnt_pos[j], 0), "joint not at the body origin"
            joint = {
                "name": name(mujoco.mjtObj.mjOBJ_JOINT, j),
                "type": "hinge" if model.jnt_type[j] == mujoco.mjtJoint.mjJNT_HINGE else "slide",
                "axis": [round(float(v), 9) for v in model.jnt_axis[j]],
                "range": [round(float(v), 9) for v in model.jnt_range[j]],
            }
        bodies.append({
            "name": name(mujoco.mjtObj.mjOBJ_BODY, b),
            "parent": name(mujoco.mjtObj.mjOBJ_BODY, int(model.body_parentid[b])),
            "pos": [round(float(v), 9) for v in model.body_pos[b]],
            "quat": [round(float(v), 9) for v in model.body_quat[b]],  # w x y z
            "joint": joint,
        })

    s = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "grasp_site")
    grasp = {
        "body": name(mujoco.mjtObj.mjOBJ_BODY, int(model.site_bodyid[s])),
        "pos": [round(float(v), 9) for v in model.site_pos[s]],
        "quat": [round(float(v), 9) for v in model.site_quat[s]],
    }

    c = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "wrist_cam")
    wrist_cam = {
        "body": name(mujoco.mjtObj.mjOBJ_BODY, int(model.site_bodyid[c])),
        "pos": [round(float(v), 9) for v in model.site_pos[c]],
        "quat": [round(float(v), 9) for v in model.site_quat[c]],
        "fovy": WRIST_FOVY,
    }

    # ---- meshes
    OUT_GLB_DIR.mkdir(parents=True, exist_ok=True)
    scene = trimesh.Scene()
    stats = []
    for g in range(model.ngeom):
        if model.geom_type[g] != mujoco.mjtGeom.mjGEOM_MESH:
            continue
        m = int(model.geom_dataid[g])
        va, vn = int(model.mesh_vertadr[m]), int(model.mesh_vertnum[m])
        fa, fn = int(model.mesh_faceadr[m]), int(model.mesh_facenum[m])
        v = np.array(model.mesh_vert[va:va + vn], dtype=np.float64)
        f = np.array(model.mesh_face[fa:fa + fn], dtype=np.int64)
        # STL meshes arrive as triangle soup; weld coincident vertices so the decimator sees a surface.
        welded = trimesh.Trimesh(vertices=v, faces=f, process=True)
        welded.merge_vertices(digits_vertex=7)
        v, f = np.asarray(welded.vertices), np.asarray(welded.faces, dtype=np.int64)
        target = max(MIN_TRIS, int(fn * KEEP))
        if len(f) > target:
            v2, f2 = fast_simplification.simplify(v.astype(np.float32), f.astype(np.int32),
                                                  target_reduction=1 - target / len(f))
        else:
            v2, f2 = v, f
        r = quat_to_mat(model.geom_quat[g])
        v2 = (np.asarray(v2, dtype=np.float64) @ r.T) + model.geom_pos[g]
        body = name(mujoco.mjtObj.mjOBJ_BODY, int(model.geom_bodyid[g]))
        mesh_name = name(mujoco.mjtObj.mjOBJ_MESH, m)
        node = f"{body}__{mesh_name}"
        tm = trimesh.Trimesh(vertices=v2.astype(np.float32), faces=np.asarray(f2), process=False)
        scene.add_geometry(tm, node_name=node, geom_name=node)
        stats.append((node, fn, len(f2)))
    glb = scene.export(file_type="glb")
    (OUT_GLB_DIR / "yam.glb").write_bytes(glb)
    shutil.copy(sdk / "LICENSE", OUT_GLB_DIR / "LICENSE")
    (OUT_GLB_DIR / "README.md").write_text(
        "# YAM v1 + linear_4310 meshes for the web twin (TDM-97)\n\n"
        f"Source: {SDK_REF} (`i2rt/robot_models/arm/yam/v1`, `i2rt/robot_models/gripper/linear_4310`), MIT, see LICENSE.\n\n"
        "Built by `spikes/tdm-97/build_yam_assets.py` from the SDK's own `combine_arm_and_gripper_xml(YAM, LINEAR_4310)`\n"
        "model compiled in MuJoCo: each mesh geom's pose is baked into its body frame, then decimated\n"
        f"(~{int(KEEP * 100)}% of the triangles, floor {MIN_TRIS}). The kinematic tree is in\n"
        "`apps/web/src/components/robotview/yamModel.ts`.\n\n"
        "| node | tris (SDK) | tris (web) |\n|---|---:|---:|\n"
        + "".join(f"| {n} | {a} | {b} |\n" for n, a, b in stats)
    )

    ts = (
        "// GENERATED by spikes/tdm-97/build_yam_assets.py; do not edit by hand.\n"
        f"// Kinematic tree of the I2RT YAM v1 + linear_4310 ({SDK_REF}, MIT), as compiled by MuJoCo from the\n"
        "// SDK's combine_arm_and_gripper_xml(YAM, LINEAR_4310): the model yam_kinematics.py and the MuJoCo sim use.\n"
        "// Body frame = parent frame * translate(pos) * rotate(quat [w,x,y,z]) * joint(axis, q).\n\n"
        "export interface YamJoint { name: string; type: 'hinge' | 'slide'; axis: [number, number, number]; range: [number, number] }\n"
        "export interface YamBody { name: string; parent: string; pos: [number, number, number]; quat: [number, number, number, number]; joint: YamJoint | null }\n\n"
        f"export const YAM_SDK_REF = {json.dumps(SDK_REF)};\n"
        "export const YAM_BODIES: YamBody[] = [\n" + "".join(f"  {json.dumps(b)},\n" for b in bodies) + "];\n\n"
        f"export const YAM_GRASP_SITE = {json.dumps(grasp)} as const;\n\n"
        "/** D405 wrist camera (looks along its local -Z, +Y up), the MuJoCo sim's `wrist` camera. */\n"
        f"export const YAM_WRIST_CAMERA = {json.dumps(wrist_cam)} as const;\n\n"
        "/** Finger slide travel (m) of joint7/joint8: 0 = open, 0.0475 = closed (matches the MuJoCo sim). */\n"
        "export const YAM_FINGER_TRAVEL = 0.0475;\n\n"
        "export const YAM_MODEL = { bodies: YAM_BODIES, grasp: YAM_GRASP_SITE, fingerTravel: YAM_FINGER_TRAVEL };\n"
    )
    OUT_TS.write_text(ts)
    for n, a, b in stats:
        print(f"{n:28s} {a:7d} -> {b:6d}")
    print(f"glb {len(glb) / 1024:.0f} KiB, bodies {len(bodies)}")


if __name__ == "__main__":
    main()
