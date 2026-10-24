"""TDM-97 screenshot pair, MuJoCo side: render the sim scene (rdf_robot_client.adapters.mujoco_scene,
the MuJoCo YAM sim's own MJCF) from its exo camera ("top") and its "wrist" camera at fixed poses,
cube and plate at the scene defaults. shoot_twin.py renders the twin at the same poses.

  uv run --no-project --python 3.11 --with mujoco==3.3.7 --with numpy --with pillow \
      python -I spikes/tdm-97/mujoco_pair.py spikes/robotview/tmp
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services/robot-client/src"))

from rdf_robot_client.adapters.mujoco_scene import ARM_JOINTS, FINGER_JOINT, FINGER_TRAVEL, build_scene_xml  # noqa: E402

POSES = json.loads((Path(__file__).parent / "pair_poses.json").read_text())
CUBE = (0.31, -0.14, 0.02)


def main() -> None:
    import mujoco
    from PIL import Image

    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    model = mujoco.MjModel.from_xml_string(build_scene_xml())
    data = mujoco.MjData(model)
    adr = lambda j: int(model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, j)])  # noqa: E731
    r = mujoco.Renderer(model, height=480, width=640)
    site = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "grasp_site")
    report = {}
    for name, pose in POSES.items():
        q, g = pose["q"], pose.get("gripper", 1.0)
        mujoco.mj_resetData(model, data)
        for j, v in zip(ARM_JOINTS, q):
            data.qpos[adr(j)] = v
        slide = (1 - g) * FINGER_TRAVEL
        data.qpos[adr(FINGER_JOINT)] = slide
        data.qpos[adr("joint8")] = slide
        c = adr("cube_free")
        data.qpos[c:c + 3] = CUBE
        data.qpos[c + 3:c + 7] = (1, 0, 0, 0)
        mujoco.mj_forward(model, data)
        report[name] = [round(float(v), 5) for v in data.site_xpos[site]]
        for cam in ("top", "wrist"):
            r.update_scene(data, camera=cam)
            Image.fromarray(r.render()).save(out / f"tdm97_{name}_{cam}_mujoco.png")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
