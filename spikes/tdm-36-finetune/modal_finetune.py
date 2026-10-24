"""TDM-36 / S-05a: pi0.5 fine-tune recipe on Modal (openpi + LeRobot).

Usage (from the repo root):
    # one-time: download pi0.5 base checkpoint + dataset into the Volume, compute norm stats
    modal run spikes/tdm-36-finetune/modal_finetune.py --action prepare
    # smoke fine-tune (writes a checkpoint to the Volume)
    modal run spikes/tdm-36-finetune/modal_finetune.py --action train --mode lora --steps 200 --gpu H100
    # memory / speed probe for full fine-tune (checkpoint goes to local disk, not the Volume)
    modal run spikes/tdm-36-finetune/modal_finetune.py --action train --mode full --steps 100 --gpu H100 --no-save-to-volume
    # load the smoke checkpoint like the policy server will and predict one action chunk
    modal run spikes/tdm-36-finetune/modal_finetune.py --action infer

Modal resources (all prefixed rdf-s05a- so they do not collide with sibling spikes):
    app     rdf-s05a-finetune
    volume  rdf-s05a-checkpoints   (/vol: openpi-cache/, lerobot/, assets/, checkpoints/, metrics/)
    secret  rdf-s05a-hf            (HF_TOKEN)
"""

from __future__ import annotations

import json
import pathlib
import time

import modal

OPENPI_COMMIT = "215abfb217dbac7d5f1273282331b9b1866c0479"  # Physical-Intelligence/openpi main, 2026-08-24
HERE = pathlib.Path(__file__).parent
VOL_PATH = "/vol"

app = modal.App("rdf-s05a-finetune")
volume = modal.Volume.from_name("rdf-s05a-checkpoints", create_if_missing=True)
hf_secret = modal.Secret.from_name("rdf-s05a-hf")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "git-lfs", "ffmpeg", "build-essential", "clang")
    .pip_install("uv==0.8.13")
    .env({"GIT_LFS_SKIP_SMUDGE": "1", "UV_PROJECT_ENVIRONMENT": "/usr/local", "UV_COMPILE_BYTECODE": "1"})
    .run_commands(
        "git clone https://github.com/Physical-Intelligence/openpi.git /openpi",
        f"cd /openpi && git checkout {OPENPI_COMMIT}",
        # lerobot is pinned by openpi's uv.lock (huggingface/lerobot@0cf8648, dataset format v2.1).
        "cd /openpi && uv sync --frozen --no-dev --inexact",
    )
    .env(
        {
            "OPENPI_DATA_HOME": f"{VOL_PATH}/openpi-cache",
            "HF_LEROBOT_HOME": f"{VOL_PATH}/lerobot",
            "HF_HUB_ENABLE_HF_TRANSFER": "0",
            "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.9",
            "TOKENIZERS_PARALLELISM": "false",
            # openpi's scripts/ is a package; must be importable by name so spawned
            # torch DataLoader workers can unpickle transforms defined there.
            "PYTHONPATH": "/openpi:/root",
        }
    )
    .add_local_file(HERE / "rdf_s05a_config.py", "/root/rdf_s05a_config.py")
)


def _dir_size_gb(p: pathlib.Path) -> float:
    return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e9, 2)


@app.function(image=image, volumes={VOL_PATH: volume}, secrets=[hf_secret], cpu=16, memory=32768, timeout=3600)
def prepare(repo_id: str = "lerobot/svla_so100_pickplace") -> dict:
    """Download pi0.5 base params + the LeRobot dataset into the Volume and compute norm stats."""
    import openpi.shared.download as download
    import openpi.shared.normalize as normalize

    import rdf_s05a_config as rc

    out: dict = {}
    t = time.time()
    # openpi prefers gsutil for openpi-assets; without it, fsspec/gcsfs with anonymous access works.
    params = download.maybe_download(rc.PI05_BASE_PARAMS, token="anon")
    out["pi05_base_params_dir"] = str(params)
    out["pi05_base_params_gb"] = _dir_size_gb(params)
    out["checkpoint_download_s"] = round(time.time() - t, 1)
    volume.commit()

    # Norm stats via openpi's own pipeline (same transforms as training: repack -> inputs -> delta actions).
    import scripts.compute_norm_stats as ns

    ds_spec = rc.SO100_PICKPLACE if repo_id == rc.SO100_PICKPLACE.repo_id else rc.yam_dataset_spec(repo_id)
    config = rc.make_train_config(mode="lora", spec=ds_spec, num_workers=14)
    data_config = config.data.create(config.assets_dirs, config.model)
    t = time.time()
    loader, num_batches = ns.create_torch_dataloader(
        data_config, config.model.action_horizon, config.batch_size, config.model, config.num_workers, None
    )
    out["dataset_download_s"] = round(time.time() - t, 1)
    stats = {k: normalize.RunningStats() for k in ("state", "actions")}
    t = time.time()
    for batch in loader:
        for k in stats:
            stats[k].update(__import__("numpy").asarray(batch[k]))
    out["norm_stats_s"] = round(time.time() - t, 1)
    out["norm_stats_batches"] = num_batches
    norm_stats = {k: s.get_statistics() for k, s in stats.items()}
    stats_dir = pathlib.Path(f"{VOL_PATH}/assets") / repo_id
    normalize.save(stats_dir, norm_stats)
    out["norm_stats_path"] = str(stats_dir)
    out["dataset_gb"] = _dir_size_gb(pathlib.Path(f"{VOL_PATH}/lerobot") / repo_id)
    out["norm_stats_preview"] = {
        k: {"mean": [round(float(x), 4) for x in v.mean[:7]], "std": [round(float(x), 4) for x in v.std[:7]]}
        for k, v in norm_stats.items()
    }
    volume.commit()
    return out


@app.function(
    image=image,
    volumes={VOL_PATH: volume},
    secrets=[hf_secret],
    gpu="H100",
    cpu=16,
    memory=98304,
    timeout=2 * 3600,
)
def train(
    mode: str = "lora",
    steps: int = 200,
    batch_size: int = 32,
    exp_name: str = "smoke",
    save_to_volume: bool = True,
    called_at: float = 0.0,
    num_workers: int = 12,
    repo_id: str = "lerobot/svla_so100_pickplace",
) -> dict:
    import logging
    import re
    import subprocess
    import threading
    import types

    started = time.time()
    import jax

    import rdf_s05a_config as rc

    gpu_name = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True
    ).stdout.strip()

    ds_spec = rc.SO100_PICKPLACE if repo_id == rc.SO100_PICKPLACE.repo_id else rc.yam_dataset_spec(repo_id)
    ckpt_base = f"{VOL_PATH}/checkpoints" if save_to_volume else "/tmp/checkpoints"
    config = rc.make_train_config(
        mode=mode,
        spec=ds_spec,
        num_train_steps=steps,
        batch_size=batch_size,
        exp_name=exp_name,
        checkpoint_base_dir=ckpt_base,
        num_workers=num_workers,
    )

    import scripts.train as tr

    # --- instrumentation: timestamp openpi's own log lines (no change to the training loop) ---
    marks: dict[str, float] = {}
    step_lines: list[tuple[float, int, str]] = []

    class _Marks(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            for key in ("Initialized data loader", "Initialized train state", "Waiting for checkpoint manager"):
                if msg.startswith(key) and key not in marks:
                    marks[key] = time.time()

    logging.basicConfig(level=logging.INFO)
    logging.getLogger().addHandler(_Marks())

    orig_tqdm = tr.tqdm.tqdm

    class _TimedTqdm(orig_tqdm):
        @classmethod
        def write(cls, s, *a, **k):
            m = re.match(r"Step (\d+):", str(s))
            if m:
                step_lines.append((time.time(), int(m.group(1)), str(s)))
            return orig_tqdm.write(s, *a, **k)

    tr.tqdm = types.SimpleNamespace(tqdm=_TimedTqdm)

    util_samples: list[tuple[float, int, int]] = []
    stop = threading.Event()

    def _sample():
        while not stop.is_set():
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            try:
                u, m = (int(x) for x in r.split(","))
                util_samples.append((time.time(), u, m))
            except ValueError:
                pass
            stop.wait(2)

    threading.Thread(target=_sample, daemon=True).start()

    t_main = time.time()
    error = None
    try:
        tr.main(config)
    except Exception as e:  # noqa: BLE001 - we want the numbers even on OOM
        error = f"{type(e).__name__}: {str(e)[:2000]}"
    t_end = time.time()
    stop.set()

    mem = jax.local_devices()[0].memory_stats() or {}
    out: dict = {
        "mode": mode,
        "gpu": gpu_name,
        "steps": steps,
        "batch_size": batch_size,
        "action_horizon": config.model.action_horizon,
        "num_workers": num_workers,
        "error": error,
        "cold_start_s": round(started - called_at, 1) if called_at else None,
        "import_s": round(t_main - started, 1),
        "peak_gpu_mem_gb": round(mem.get("peak_bytes_in_use", 0) / 1e9, 2),
        "gpu_mem_limit_gb": round(mem.get("bytes_limit", 0) / 1e9, 2),
        "total_main_s": round(t_end - t_main, 1),
    }
    if "Initialized data loader" in marks:
        out["dataloader_init_s"] = round(marks["Initialized data loader"] - t_main, 1)
    if "Initialized train state" in marks and "Initialized data loader" in marks:
        out["weights_load_s"] = round(marks["Initialized train state"] - marks["Initialized data loader"], 1)
    if step_lines:
        first_t = step_lines[0][0]
        if "Initialized train state" in marks:
            out["first_step_incl_compile_s"] = round(first_t - marks["Initialized train state"], 1)
        steady = [x for x in step_lines if x[1] >= 20] or step_lines
        if len(steady) >= 2:
            dt = steady[-1][0] - steady[0][0]
            ds = steady[-1][1] - steady[0][1]
            out["sec_per_step"] = round(dt / ds, 3)
            out["steps_per_sec"] = round(ds / dt, 3)
            window = [u for (ts, u, _m) in util_samples if steady[0][0] <= ts <= steady[-1][0]]
            if window:
                out["mean_gpu_util_pct_steady"] = round(sum(window) / len(window), 1)
        if "Waiting for checkpoint manager" in marks:
            out["checkpoint_save_s"] = round(t_end - step_lines[-1][0], 1)
        out["loss_first"] = step_lines[0][2]
        out["loss_last"] = step_lines[-1][2]
    if util_samples:
        out["max_nvidia_smi_mem_used_mib"] = max(m for (_t, _u, m) in util_samples)

    ckpt_dir = pathlib.Path(config.checkpoint_dir)
    if ckpt_dir.exists():
        out["checkpoint_dir"] = str(ckpt_dir)
        out["checkpoint_steps"] = sorted(p.name for p in ckpt_dir.iterdir() if p.name.isdigit())
        out["checkpoint_gb"] = _dir_size_gb(ckpt_dir)

    metrics_dir = pathlib.Path(f"{VOL_PATH}/metrics")
    metrics_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{mode}_{gpu_name.split(',')[0].replace(' ', '-')}_{exp_name}_{int(started)}"
    (metrics_dir / f"{tag}.json").write_text(json.dumps(out, indent=2))
    volume.commit()
    return out


@app.function(image=image, volumes={VOL_PATH: volume}, secrets=[hf_secret], gpu="L40S", cpu=8, memory=65536, timeout=1800)
def infer_smoke(
    ckpt_dir: str = f"{VOL_PATH}/checkpoints/rdf_s05a_pi05_lora/smoke_lora_h100/199",
    mode: str = "lora",
    repo_id: str = "lerobot/svla_so100_pickplace",
    frame: int = 200,
) -> dict:
    """Load the fine-tuned checkpoint the way the policy server will and predict one action chunk."""
    import lerobot.common.datasets.lerobot_dataset as lds
    import numpy as np
    import openpi.policies.policy_config as policy_config

    import rdf_s05a_config as rc

    ds_spec = rc.SO100_PICKPLACE if repo_id == rc.SO100_PICKPLACE.repo_id else rc.yam_dataset_spec(repo_id)
    config = rc.make_train_config(mode=mode, spec=ds_spec)
    t = time.time()
    policy = policy_config.create_trained_policy(config, ckpt_dir)
    load_s = time.time() - t

    ds = lds.LeRobotDataset(repo_id)
    item = ds[frame]
    # Exactly the payload a robot client sends (post-repack keys; images HWC uint8 or CHW float).
    obs = {
        "observation/image": item[ds_spec.base_image_key].numpy(),
        "observation/wrist_image": item[ds_spec.wrist_image_key].numpy(),
        "observation/state": item[ds_spec.state_key].numpy(),
        "prompt": item["task"],
    }
    times = []
    for _ in range(4):
        t = time.time()
        out = policy.infer(obs)
        times.append(time.time() - t)
    actions = np.asarray(out["actions"])
    ep_end = int(ds.episode_data_index["to"][item["episode_index"]].item())
    horizon = min(actions.shape[0], ep_end - frame)
    gt = np.stack([ds[frame + i][ds_spec.action_key].numpy() for i in range(horizon)])
    return {
        "ckpt_dir": ckpt_dir,
        "policy_load_s": round(load_s, 1),
        "infer_s_first_incl_compile": round(times[0], 2),
        "infer_s_warm": round(float(np.median(times[1:])), 3),
        "action_chunk_shape": list(actions.shape),
        "state": [round(float(x), 2) for x in obs["observation/state"]],
        "pred_first_action": [round(float(x), 2) for x in actions[0]],
        "gt_first_action": [round(float(x), 2) for x in gt[0]],
        "mae_vs_gt_chunk": round(float(np.abs(actions[:horizon] - gt).mean()), 3),
        "mae_hold_still_baseline": round(float(np.abs(gt - obs["observation/state"][None]).mean()), 3),
    }


@app.local_entrypoint()
def main(
    action: str = "train",
    mode: str = "lora",
    steps: int = 200,
    gpu: str = "H100",
    batch_size: int = 32,
    exp_name: str = "",
    save_to_volume: bool = True,
    num_workers: int = 12,
):
    if action == "prepare":
        print(json.dumps(prepare.remote(), indent=2))
        return
    if action == "infer":
        print(json.dumps(infer_smoke.remote(), indent=2))
        return
    exp = exp_name or f"smoke_{mode}_{gpu.lower().replace('-', '')}"
    fn = train.with_options(gpu=gpu)
    res = fn.remote(
        mode=mode,
        steps=steps,
        batch_size=batch_size,
        exp_name=exp,
        save_to_volume=save_to_volume,
        called_at=time.time(),
        num_workers=num_workers,
    )
    print(json.dumps(res, indent=2))
