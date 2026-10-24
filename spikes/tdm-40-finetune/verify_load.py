"""TDM-40 proof: load a fine-tune output checkpoint the way the policy server does and predict a chunk.

    cd services/training && uv run modal run ../../spikes/tdm-40-finetune/verify_load.py \
        --run-dir runs/ticket-tdm40-smoke/<run>

Loads twice: (a) TDM-41's path (``rdf_training.policy_server.infer_spec`` + ``make_openpi_config`` +
``create_trained_policy``) when that module is present, and (b) ``finetune_openpi.load_policy(rdf_run.json)``.
Both get the same post-repack observation from the training dataset; prints shapes, timings and the
chunk MAE vs the recorded actions. rdf-checkpoints and rdf-datasets are mounted read-only.
"""
import json
import pathlib
import time

import modal

OPENPI_COMMIT = "215abfb217dbac7d5f1273282331b9b1866c0479"
_HERE = pathlib.Path(__file__).resolve()
REPO = _HERE.parents[2] if len(_HERE.parents) > 2 else _HERE.parent  # inside the container: /root

app = modal.App("rdf-tdm40-verify")
image = (  # TDM-38's image, step for step (cached layers)
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "git-lfs", "ffmpeg", "build-essential", "clang")
    .pip_install("uv==0.8.13")
    .env({"GIT_LFS_SKIP_SMUDGE": "1", "UV_PROJECT_ENVIRONMENT": "/usr/local", "UV_COMPILE_BYTECODE": "1"})
    .run_commands(
        "git clone https://github.com/Physical-Intelligence/openpi.git /openpi",
        f"cd /openpi && git checkout {OPENPI_COMMIT}",
        "cd /openpi && uv sync --frozen --no-dev --inexact",
    )
    .env({"OPENPI_DATA_HOME": "/checkpoints/openpi-cache", "HF_LEROBOT_HOME": "/datasets/lerobot",
          "HF_HUB_ENABLE_HF_TRANSFER": "0", "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.9",
          "TOKENIZERS_PARALLELISM": "false", "PYTHONPATH": "/openpi:/root"})
    .add_local_dir(REPO / "services/training/src/rdf_training", "/root/rdf_training", ignore=["**/__pycache__"])
    .add_local_dir(REPO / "services/common/src/rdf_common", "/root/rdf_common", ignore=["**/__pycache__"])
)


@app.function(image=image, gpu=["L40S", "A100-40GB", "H100"], cpu=4, memory=32768, timeout=1200,
              volumes={"/checkpoints": modal.Volume.from_name("rdf-checkpoints").read_only(),
                       "/datasets": modal.Volume.from_name("rdf-datasets").read_only()})
def verify(run_dir: str, frame: int = 100) -> dict:
    import importlib.util

    import lerobot.common.datasets.lerobot_dataset as lds
    import numpy as np
    import openpi.policies.policy_config as policy_config

    from rdf_training import finetune_openpi as fo

    run = pathlib.Path("/checkpoints") / run_dir
    meta = json.loads((run / "rdf_run.json").read_text())
    step_dir = meta["checkpoint_step_dir"]
    spec = meta["dataset_spec"]
    ds = lds.LeRobotDataset(spec["repo_id"])
    item = ds[frame]
    obs = {"observation/image": item[spec["base_image_key"]].numpy(),
           "observation/wrist_image": item[spec["wrist_image_key"]].numpy(),
           "observation/state": item[spec["state_key"]].numpy(), "prompt": item["task"]}
    ep_end = int(ds.episode_data_index["to"][item["episode_index"]].item())
    gt = np.stack([ds[frame + i][spec["action_key"]].numpy() for i in range(min(50, ep_end - frame))])

    def run_policy(name, make):
        t = time.time()
        policy = make()
        load_s = time.time() - t
        times = []
        for _ in range(3):
            t = time.time()
            a = np.asarray(policy.infer(obs)["actions"])
            times.append(time.time() - t)
        return {"loader": name, "load_s": round(load_s, 1), "first_infer_s": round(times[0], 2),
                "warm_infer_s": round(float(np.median(times[1:])), 3), "chunk_shape": list(a.shape),
                "mae_vs_recorded": round(float(np.abs(a[: len(gt)] - gt).mean()), 4),
                "mae_hold_still": round(float(np.abs(gt - obs["observation/state"][None]).mean()), 4)}

    out = {"checkpoint_step_dir": step_dir, "files": sorted(p.name for p in pathlib.Path(step_dir).iterdir())}
    if importlib.util.find_spec("rdf_training.policy_server") is not None:
        from rdf_training import policy_server as ps

        pspec = ps.infer_spec(step_dir)
        out["tdm41_spec"] = {k: getattr(pspec, k) for k in ("layout", "state_dim", "action_dim", "joint_dims",
                                                           "asset_id", "mode", "action_horizon")}
        out["tdm41"] = run_policy("policy_server", lambda: policy_config.create_trained_policy(
            ps.make_openpi_config(pspec), step_dir))
    out["tdm40"] = run_policy("finetune_openpi.load_policy", lambda: fo.load_policy(run))
    return out


@app.local_entrypoint()
def main(run_dir: str):
    print(json.dumps(verify.remote(run_dir), indent=2))
