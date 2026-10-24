"""TDM-97 FK spot check, reference side: tool point (grasp_site) for 3 poses from
  * rdf_robot_client.adapters.yam_kinematics.PoeKinematics (the adapter's FK), and
  * MuJoCo on the SDK-combined model (what the sim renders),
printed as JSON for apps/web/test/robotview-twin-fk.test.ts.

  uv run --no-project --python 3.11 --with mujoco==3.3.7 --with numpy --with pyyaml \
      python -I spikes/tdm-97/fk_reference.py spikes/tdm-97/tmp/i2rt
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "services/robot-client/src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from rdf_robot_client.adapters.yam_kinematics import PoeKinematics  # noqa: E402

POSES = {
    "rest": [0, 0, 0, 0, 0, 0],
    "ready": [0, 0.98, 0.93, -0.73, 0, 0],  # YAM_READY (yam-sdk-notes.md section 4)
    "reach_left": [0.6, 1.4, 1.1, -0.4, 0.5, 0.8],
}


def main() -> None:
    import mujoco
    from build_yam_assets import combined_xml

    model = mujoco.MjModel.from_xml_string(combined_xml(Path(sys.argv[1]).resolve()))
    data = mujoco.MjData(model)
    site = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "grasp_site")
    poe = PoeKinematics("linear_4310")
    out = {}
    for name, q in POSES.items():
        t = poe.fk(q)
        data.qpos[:] = 0
        data.qpos[:6] = q
        mujoco.mj_forward(model, data)
        out[name] = {
            "q": q,
            "poe_pos": [round(float(v), 7) for v in t[:3, 3]],
            "poe_z_axis": [round(float(v), 7) for v in t[:3, 2]],
            "mujoco_pos": [round(float(v), 7) for v in data.site_xpos[site]],
        }
        d = np.linalg.norm(t[:3, 3] - data.site_xpos[site])
        print(f"# {name}: poe vs mujoco {d * 1000:.4f} mm", file=sys.stderr)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
