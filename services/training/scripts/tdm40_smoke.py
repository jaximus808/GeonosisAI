"""TDM-40 smoke: a fixture-converted YAM-shaped dataset on rdf-datasets, then a short fine-tune.

    cd services/training
    uv run python scripts/tdm40_smoke.py build            # 5 labelled fixture demos -> TDM-39 convert -> rdf-datasets
    uv run modal run modal_app.py --action finetune --ticket-id tdm40-smoke \
        --dataset-uri modal-vol://rdf-datasets/lerobot/rdf-fixtures/tdm40-smoke \
        --hyperparams '{"steps": 200, "smoke": true, "norm_stats": "recompute"}'
    uv run python scripts/tdm40_smoke.py watch [ticket-id] # live progress from the Modal Dict

Real YAM data instead of the fixture (TDM-92 found it; left arm of a bimanual YAM, "Fold the cloth"):
    cd services/orchestrator && uv run python -m rdf_orchestrator.dataset_import hf://allenai/24112025-yam-01 \
        --out ../../spikes/tdm-40-finetune/tmp/yam-import --episodes 0,1 --arm left
    cd ../training && uv run python scripts/tdm40_smoke.py build-import ../../spikes/tdm-40-finetune/tmp/yam-import
    # -> dataset_uri=modal-vol://rdf-datasets/lerobot/rdf-imports/tdm40-yam-allenai-left

The data is TDM-39's synthetic fixture (``fixture: true``, never robot data): it proves the pipeline
(convert -> norm stats -> LoRA train -> checkpoint the policy server loads), not policy quality.
For the real run, pass the dataset_uri TDM-39's ``convert`` returned for the ticket's accepted demos.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "spikes" / "tdm-40-finetune" / "tmp"
REPO_ID = "rdf-fixtures/tdm40-smoke"
IMPORT_REPO_ID = "rdf-imports/tdm40-yam-allenai-left"
N_EPISODES, N_FRAMES = 5, 300  # 5 x 10 s demos at 30 fps (the real plan: 3-5 clean demos)


def build() -> None:
    sys.path.insert(0, str(REPO / "services" / "training" / "src"))
    from rdf_training.convert_fixtures import FIXTURE_T0_US, FIXTURE_TASK, write_fixture_episode

    eps_dir, ds_root = OUT / "fixture-episodes", OUT / "datasets"
    subprocess.run(["rm", "-rf", str(eps_dir), str(ds_root)], check=True)
    eps = [write_fixture_episode(eps_dir, episode_id=f"tdm40-clean-{i}", kind="clean_teleop", n_frames=N_FRAMES,
                                 t0_us=FIXTURE_T0_US + i * 60_000_000) for i in range(N_EPISODES)]
    _convert_and_upload({"repo_id": REPO_ID, "task": FIXTURE_TASK, "episodes": eps, "allow_fixtures": True}, ds_root)


def build_import(import_dir: str) -> None:
    """Episodes written by rdf_orchestrator.dataset_import (our format) -> TDM-39 convert -> rdf-datasets."""
    sys.path.insert(0, str(REPO / "services" / "training" / "src"))
    from rdf_common.episode_format import read_actions

    eps, tasks = [], set()
    for d in sorted(p for p in Path(import_dir).resolve().iterdir() if (p / "actions.parquet").exists()):
        tasks.add(read_actions(d / "actions.parquet").meta["source"]["task"])
        eps.append({"episode_id": f"tdm40-{d.name}", "kind": "clean_teleop", "actions_uri": str(d / "actions.parquet"),
                    "cameras": {c: str(d / f"{c}.mp4") for c in ("exo", "wrist") if (d / f"{c}.mp4").exists()}})
    if len(tasks) != 1:
        raise SystemExit(f"want one task across episodes, got {tasks}")
    ds_root = OUT / "datasets-import"
    subprocess.run(["rm", "-rf", str(ds_root)], check=True)
    _convert_and_upload({"repo_id": IMPORT_REPO_ID, "task": tasks.pop(), "episodes": eps}, ds_root)


def _convert_and_upload(manifest: dict, ds_root: Path) -> None:
    from rdf_training.convert import convert

    repo_id = manifest["repo_id"]
    report = convert(manifest, lerobot_root=ds_root / "lerobot", assets_root=ds_root / "assets")
    print(json.dumps({k: report.get(k) for k in ("repo_id", "frames", "norm_stats_path")}, default=str))
    for sub in ("lerobot", "assets"):
        src = ds_root / sub / repo_id
        subprocess.run(["modal", "volume", "rm", "-r", "rdf-datasets", f"/{sub}/{repo_id}"], check=False,
                       capture_output=True)
        subprocess.run(["modal", "volume", "put", "rdf-datasets", str(src), f"/{sub}/{repo_id}"], check=True)
    print(f"dataset_uri=modal-vol://rdf-datasets/lerobot/{repo_id}")


def watch(ticket_id: str = "tdm40-smoke") -> None:
    import modal

    d = modal.Dict.from_name("rdf-training-progress", create_if_missing=True)
    last = None
    while True:
        p = d.get(f"ticket-{ticket_id}/latest")
        if p and p.get("updated_at") != last:
            last = p["updated_at"]
            print(json.dumps({k: p.get(k) for k in ("status", "phase", "step", "total_steps", "loss", "sec_per_step",
                                                    "eta_s", "checkpoint_uri", "error")}), flush=True)
            if p.get("status") in ("done", "failed"):
                return
        time.sleep(10)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    {"build": build, "build-import": build_import, "watch": watch}[cmd](*sys.argv[2:])
