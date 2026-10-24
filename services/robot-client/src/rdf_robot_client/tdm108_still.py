"""TDM-108: one offscreen MuJoCo still of a scene spec, for the job-drop share card.

    MUJOCO_GL=cgl python -m rdf_robot_client.tdm108_still --spec spec.json --size 438x260 --out still.png

Builds the same MJCF the [SIM] robot runs (``adapters.mujoco_scene.build_scene_xml``), puts the arm at
the sim's home pose and every object at its spec pose, and renders the spec's ``top`` camera (or
``--camera``) at exactly the requested size, so the card's picture well is filled with no cropping.
No physics is stepped and no robot or network is touched: one ``mj_forward`` and one render.

The orchestrator calls this in a subprocess with this project's venv (``job_drops.render_still``) and
caches the PNG per (spec, size), so a scene is rendered once, not once per post.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path


def render(spec: dict, sizes: list[tuple[int, int]], camera: str = "top") -> list:
    """One model load, one RGB array per (width, height)."""
    if "MUJOCO_GL" not in os.environ:
        os.environ["MUJOCO_GL"] = "cgl" if sys.platform == "darwin" else "egl"
    import mujoco
    import numpy as np

    from .adapters.mujoco_scene import ARM_JOINTS, build_scene_xml, load_scene
    from .adapters.mujoco_sim import SIM_HOME

    m = mujoco.MjModel.from_xml_string(build_scene_xml(load_scene(spec)))
    m.vis.global_.offwidth = max([int(m.vis.global_.offwidth)] + [w for w, _ in sizes])
    m.vis.global_.offheight = max([int(m.vis.global_.offheight)] + [h for _, h in sizes])
    d = mujoco.MjData(m)
    for j, q in zip(ARM_JOINTS, SIM_HOME):
        d.qpos[m.joint(j).qposadr[0]] = q
    mujoco.mj_forward(m, d)
    cid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_CAMERA, camera)
    cam = camera if cid >= 0 else -1
    fovy0 = float(m.cam_fovy[cid]) if cid >= 0 else 45.0
    out = []
    for w, h in sizes:
        if cid >= 0 and w / h > 2.6:  # a very wide strip: narrow the vertical fov a little (keeps a 2.6:1 framing)
            m.cam_fovy[cid] = math.degrees(2 * math.atan(math.tan(math.radians(fovy0) / 2) * 2.6 / (w / h)))
        elif cid >= 0:
            m.cam_fovy[cid] = fovy0
        r = mujoco.Renderer(m, h, w)
        try:
            r.update_scene(d, camera=cam)
            out.append(np.ascontiguousarray(r.render()))
        finally:
            r.close()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", required=True, help="scene spec JSON file")
    ap.add_argument("--size", action="append", required=True, help="WxH, repeatable (one model load for all)")
    ap.add_argument("--out", action="append", required=True, help="PNG path per --size")
    ap.add_argument("--camera", default="top")
    a = ap.parse_args(argv)
    if len(a.size) != len(a.out):
        ap.error("one --out per --size")
    import cv2

    spec = json.loads(Path(a.spec).read_text())
    sizes = [tuple(int(v) for v in s.lower().split("x")) for s in a.size]
    for img, out in zip(render(spec, sizes, a.camera), a.out):
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{out}.tmp.png"
        cv2.imwrite(tmp, img[:, :, ::-1])  # RGB -> BGR for OpenCV
        os.replace(tmp, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
