"""TDM-122: build the committed real-YAM excerpt fixture used by test_tdm122_yam_mapping.py.

Inputs (already on this laptop from TDM-40/92, not committed; gitignored tmp/):
  * spikes/tdm-40-finetune/tmp/yam-import/.cache/allenai__24112025-yam-01/main/meta/info.json
      the source dataset's own LeRobot v3.0 info.json (bimanual, 14-dim)
  * spikes/tdm-40-finetune/tmp/yam-import/ep000001/actions.parquet
      episode 1, left arm, imported by rdf_orchestrator.dataset_import (our episode format)
  * spikes/tdm-40-finetune/tmp/datasets-import/lerobot/rdf-imports/tdm40-yam-allenai-left/meta/info.json
      what TDM-39 convert wrote for training (LeRobot v2.1, 7-dim)
  * the real smoke checkpoint's sidecar + norm stats, fetched (free, no GPU) with
      modal volume get rdf-checkpoints runs/ticket-tdm40-yam-smoke/smoke-20261024-081135-ed9bc2/199/rdf_policy.json
      modal volume get rdf-checkpoints .../199/assets/rdf-imports/tdm40-yam-allenai-left/norm_stats.json

    cd services/orchestrator && uv run python ../../spikes/tdm-122/make_fixture.py <dir with rdf_policy.json + norm_stats.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
TMP = REPO / "spikes" / "tdm-40-finetune" / "tmp"
OUT = REPO / "services" / "robot-client" / "tests" / "fixtures" / "tdm122_allenai_yam01_excerpt.json"
ROWS = (1050, 1170)  # 4 s of episode 1, incl. the tick where joint 4's command reaches -1.674 rad (row 1107)


def main(ckpt_dir: str) -> None:
    ck = Path(ckpt_dir)
    src_info = json.loads((TMP / "yam-import/.cache/allenai__24112025-yam-01/main/meta/info.json").read_text())
    conv_info = json.loads((TMP / "datasets-import/lerobot/rdf-imports/tdm40-yam-allenai-left/meta/info.json").read_text())
    t = pq.read_table(TMP / "yam-import/ep000001/actions.parquet")
    meta = json.loads(t.schema.metadata[b"rdf.episode"])
    a, b = ROWS
    cols = {k: t.column(k).to_pylist()[a:b] for k in ("frame_index", "joint_pos", "gripper_pos", "joint_cmd", "gripper_cmd")}
    rnd = lambda v: [round(float(x), 6) for x in v] if isinstance(v, list) else round(float(v), 6)  # noqa: E731
    out = {
        "_about": "TDM-122 fixture: REAL data excerpts (hf://allenai/24112025-yam-01, left arm of a bimanual YAM, "
                  "'Fold the cloth') and the REAL TDM-40 smoke checkpoint's sidecar + norm stats. "
                  "Built by spikes/tdm-122/make_fixture.py.",
        "source_info": {
            "codebase_version": src_info["codebase_version"], "robot_type": src_info["robot_type"], "fps": src_info["fps"],
            "features": {k: {kk: v[kk] for kk in ("dtype", "shape", "names") if kk in v}
                         for k, v in src_info["features"].items() if k.startswith(("observation", "action"))},
        },
        "converted_info": {
            "codebase_version": conv_info["codebase_version"], "robot_type": conv_info["robot_type"], "fps": conv_info["fps"],
            "features": {k: {kk: v[kk] for kk in ("dtype", "shape", "names") if kk in v}
                         for k, v in conv_info["features"].items() if k.startswith(("observation", "action"))},
        },
        "import_meta": {k: meta[k] for k in ("fps", "embodiment", "joint_names", "cameras")}
                       | {"source": {k: meta["source"][k] for k in ("dataset", "episode_index", "arm", "task",
                                                                    "joint_channels", "gripper_channel", "units")}},
        "checkpoint": {
            "uri": "modal-vol://rdf-checkpoints/runs/ticket-tdm40-yam-smoke/smoke-20261024-081135-ed9bc2/199",
            "rdf_policy": json.loads((ck / "rdf_policy.json").read_text()),
            "norm_stats": json.loads((ck / "norm_stats.json").read_text()),
        },
        "episode_rows": {"episode_index": 1, "rows": list(ROWS), **{k: [rnd(v) for v in vs] for k, vs in cols.items()}},
    }
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main(sys.argv[1])
