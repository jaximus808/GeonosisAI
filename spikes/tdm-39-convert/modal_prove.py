"""TDM-39 proof: convert labelled YAM-shaped fixture episodes inside the openpi image, then read the
result with LeRobot (openpi's pin, default torchcodec decoder) and with openpi's own training data
loader (S-05a config, our norm_stats.json), and compare our norm stats with openpi's.

    cd services/training && uv run modal run ../../spikes/tdm-39-convert/modal_prove.py

CPU only (no GPU, no Volumes, no secrets): everything is written to the container's /tmp.
The image is TDM-38's / S-05a's openpi image step for step, so its layers come from Modal's cache.
"""
import json
import pathlib
import time

import modal

OPENPI_COMMIT = "215abfb217dbac7d5f1273282331b9b1866c0479"
HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[2] if len(HERE.parents) > 2 else pathlib.Path("/")  # remote: module is /root/modal_prove.py

app = modal.App("rdf-tdm39-convert-proof")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "git-lfs", "ffmpeg", "build-essential", "clang")
    .pip_install("uv==0.8.13")
    .env({"GIT_LFS_SKIP_SMUDGE": "1", "UV_PROJECT_ENVIRONMENT": "/usr/local", "UV_COMPILE_BYTECODE": "1"})
    .run_commands(
        "git clone https://github.com/Physical-Intelligence/openpi.git /openpi",
        f"cd /openpi && git checkout {OPENPI_COMMIT}",
        "cd /openpi && uv sync --frozen --no-dev --inexact",
    )
    .env({"HF_LEROBOT_HOME": "/tmp/lerobot", "TOKENIZERS_PARALLELISM": "false", "PYTHONPATH": "/openpi:/root",
          "JAX_PLATFORMS": "cpu"})
    .add_local_dir(REPO / "services/training/src/rdf_training", "/root/rdf_training", ignore=["**/__pycache__"])
    .add_local_dir(REPO / "services/common/src/rdf_common", "/root/rdf_common", ignore=["**/__pycache__"])
    .add_local_file(REPO / "spikes/tdm-36-finetune/rdf_s05a_config.py", "/root/rdf_s05a_config.py")
)


@app.function(image=image, cpu=8, memory=16384, timeout=1800)
def prove(size_h: int = 480, size_w: int = 640, batch_size: int = 8) -> dict:
    import numpy as np

    from rdf_training import convert as cv
    from rdf_training import convert_fixtures as fx

    out: dict = {}
    t = time.time()
    manifest = fx.write_fixture_manifest(pathlib.Path("/tmp/episodes"), size=(size_h, size_w))
    report = cv.convert(manifest, lerobot_root="/tmp/lerobot", assets_root="/tmp/assets")
    out["convert_s"] = round(time.time() - t, 1)
    out["convert"] = {k: report[k] for k in ("repo_id", "total_episodes", "total_frames", "video_keys", "fps")}
    out["episodes"] = [{k: e[k] for k in ("episode_id", "kind", "src_frames", "frames", "cut_before_takeover",
                                          "cut_after_handback")} for e in report["episodes"]]
    repo_id = report["repo_id"]

    # 1. LeRobot at openpi's pin, default decoder (torchcodec on linux), S-05a action chunks
    import lerobot.common.datasets.lerobot_dataset as lds

    ds = lds.LeRobotDataset(repo_id, delta_timestamps={"action": [i / 30 for i in range(50)]})
    item = ds[90]
    img = (item["observation.images.top"].permute(1, 2, 0).numpy() * 255).astype(np.uint8)
    out["lerobot"] = {
        "codebase_version": ds.meta.info["codebase_version"], "video_backend": ds.video_backend,
        "num_episodes": ds.num_episodes, "num_frames": ds.num_frames, "fps": ds.fps,
        "item90_shapes": {k: list(v.shape) for k, v in item.items() if hasattr(v, "shape")},
        "item90_task": item["task"], "item90_top_frame_number": fx.read_frame_number(img),
    }

    # 2. openpi's training data loader with the S-05a config and OUR norm stats
    import openpi.training.data_loader as dl

    import rdf_s05a_config as rc

    spec = rc.yam_dataset_spec(repo_id)
    config = rc.make_train_config(mode="lora", spec=spec, batch_size=batch_size, assets_dir="/tmp/assets",
                                  checkpoint_base_dir="/tmp/ckpt", num_workers=0)
    t = time.time()
    loader = dl.create_data_loader(config, shuffle=True, num_batches=2)
    batches = []
    for obs, actions in loader:
        batches.append({
            "images": {k: list(v.shape) for k, v in obs.images.items()},
            "image_masks": {k: [bool(x) for x in np.asarray(v)[:2]] for k, v in obs.image_masks.items()},
            "state": list(obs.state.shape), "actions": list(actions.shape),
            "tokenized_prompt": list(obs.tokenized_prompt.shape) if obs.tokenized_prompt is not None else None,
            "state_abs_max": round(float(np.abs(np.asarray(obs.state)).max()), 3),
            "actions_abs_max": round(float(np.abs(np.asarray(actions)).max()), 3),
        })
    out["openpi_loader_s"] = round(time.time() - t, 1)
    out["openpi_batches"] = batches

    # 3. our norm stats vs openpi's compute_norm_stats pipeline on the same data
    import openpi.shared.normalize as normalize
    import scripts.compute_norm_stats as ns

    data_config = config.data.create(config.assets_dirs, config.model)
    tl, _ = ns.create_torch_dataloader(data_config, config.model.action_horizon, 1, config.model, 0, None)
    stats = {k: normalize.RunningStats() for k in ("state", "actions")}
    for b in tl:
        for k in stats:
            stats[k].update(np.asarray(b[k]))
    theirs = {k: s.get_statistics() for k, s in stats.items()}
    ours = normalize.load(pathlib.Path("/tmp/assets") / repo_id)
    out["norm_stats_max_abs_diff"] = {
        k: {f: round(float(np.abs(np.asarray(getattr(ours[k], f)) - np.asarray(getattr(theirs[k], f))).max()), 6)
            for f in ("mean", "std", "q01", "q99")} for k in ("state", "actions")}
    return out


@app.local_entrypoint()
def main():
    print(json.dumps(prove.remote(), indent=2))
