"""TDM-40: the openpi half of the fine-tune (import inside the Modal openpi image only).

``rdf_training.finetune.run_finetune`` validates inputs and handles paths/progress; this module turns
the resolved settings into an openpi ``TrainConfig`` (on top of TDM-38's ``openpi_config``, the S-05a
recipe), computes norm stats with openpi's own pipeline, and runs ``scripts/train.py``'s ``main``
unchanged, hooking only its logging to stream the loss.

TDM-41 (policy server): ``load_policy("/checkpoints/runs/ticket-<t>/<run>/rdf_run.json")`` rebuilds the
exact config the run trained with (spec, prompt override, LoRA rank) and returns openpi's policy.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import subprocess
import re
import threading
import time
import types
from typing import Any, Mapping

import numpy as np

import openpi.models.gemma as _gemma
import openpi.training.config as _config
import openpi.training.weight_loaders as weight_loaders

from rdf_training import openpi_config as oc
from rdf_training.finetune import OPENPI_LORA_RANK, FinetuneSettings, LossCurve, ProgressReporter

log = logging.getLogger("rdf_training.finetune")
_STEP_RE = re.compile(r"^Step (\d+): (.*)$")
_KV_RE = re.compile(r"(\w+)=([-+0-9.eEnaif]+)")

# ------------------------------------------------------------------ LoRA rank

_ORIG_GET_CONFIG = _gemma.get_config


def apply_lora_rank(rank: int) -> None:
    """openpi hard-codes rank 16 (gemma_2b_lora) / 32 (gemma_300m_lora). For another ``lora_rank`` patch
    ``gemma.get_config`` so the PaliGemma LoRA uses ``rank`` and the action expert ``2 * rank`` (the same
    ratio), alpha = rank. Must be applied before the model is built, in training AND in the policy server."""
    if rank in (0, OPENPI_LORA_RANK):
        _gemma.get_config = _ORIG_GET_CONFIG
        return

    def get_config(variant):
        c = _ORIG_GET_CONFIG(variant)
        r = {"gemma_2b_lora": rank, "gemma_300m_lora": 2 * rank}.get(variant)
        if r is None:
            return c
        return dataclasses.replace(c, lora_configs={k: v.replace(rank=r, alpha=float(r)) for k, v in c.lora_configs.items()})

    _gemma.get_config = get_config


# ------------------------------------------------------------------ data config


@dataclasses.dataclass(frozen=True)
class OverridePrompt:
    """Replace the dataset's task string with run_settings.prompt (training and inference alike)."""

    prompt: str

    def __call__(self, data: dict) -> dict:
        return {**data, "prompt": self.prompt}


@dataclasses.dataclass(frozen=True)
class RdfSingleArmDataConfig(oc.LeRobotSingleArmDataConfig):
    prompt_override: str = ""

    def create(self, assets_dirs: pathlib.Path, model_config) -> _config.DataConfig:
        cfg = super().create(assets_dirs, model_config)
        if self.prompt_override:
            cfg = dataclasses.replace(cfg, data_transforms=cfg.data_transforms.push(inputs=[OverridePrompt(self.prompt_override)]))
        return cfg


def build_config(*, spec: Mapping[str, Any], settings: FinetuneSettings | Mapping[str, Any], config_name: str,
                 exp_name: str, checkpoint_base_dir: str, assets_dir: str) -> _config.TrainConfig:
    """run_settings -> openpi TrainConfig. checkpoint_dir = <checkpoint_base_dir>/<config_name>/<exp_name>."""
    s = settings if isinstance(settings, FinetuneSettings) else FinetuneSettings(**settings)
    ds = oc.DatasetSpec(**spec)
    apply_lora_rank(s.lora_rank)
    base = oc.make_train_config(
        mode=s.mode, spec=ds, num_train_steps=s.steps, batch_size=s.batch_size, action_horizon=s.action_horizon,
        assets_dir=assets_dir, checkpoint_base_dir=checkpoint_base_dir, exp_name=exp_name,
        num_workers=s.num_workers, log_interval=s.log_interval, peak_lr=s.lr,
    )
    return dataclasses.replace(
        base,
        name=config_name,
        weight_loader=weight_loaders.CheckpointWeightLoader(s.base_params),
        data=RdfSingleArmDataConfig(
            repo_id=ds.repo_id, spec=ds, prompt_override=s.prompt,
            assets=_config.AssetsConfig(assets_dir=assets_dir, asset_id=ds.repo_id),
            base_config=_config.DataConfig(prompt_from_task=True),
        ),
    )


# ------------------------------------------------------------------ norm stats


def compute_norm_stats(config: _config.TrainConfig, *, batch_size: int = 8, num_workers: int = 8) -> dict:
    """openpi's scripts/compute_norm_stats.py for this config, written to <assets_dir>/<repo_id>/.
    Returns timing and the max |diff| against a pre-existing file (TDM-39's), if there was one."""
    import openpi.shared.normalize as normalize
    import scripts.compute_norm_stats as ns

    data_config = config.data.create(config.assets_dirs, config.model)
    out_dir = pathlib.Path(config.data.assets.assets_dir) / data_config.asset_id
    previous = None
    if (out_dir / "norm_stats.json").exists():
        try:
            previous = normalize.load(out_dir)
        except Exception:  # noqa: BLE001
            previous = None
    t = time.time()
    loader, num_batches = ns.create_torch_dataloader(
        data_config, config.model.action_horizon, batch_size, config.model, num_workers, None)
    stats = {k: normalize.RunningStats() for k in ("state", "actions")}
    for batch in loader:
        for k in stats:
            stats[k].update(np.asarray(batch[k]))
    norm = {k: v.get_statistics() for k, v in stats.items()}
    normalize.save(out_dir, norm)
    out = {"norm_stats_s": round(time.time() - t, 1), "norm_stats_batches": num_batches,
           "norm_stats_path": str(out_dir / "norm_stats.json")}
    if previous:
        diffs = [float(np.max(np.abs(np.asarray(getattr(norm[k], f)) - np.asarray(getattr(previous[k], f)))))
                 for k in norm if k in previous for f in ("mean", "std")
                 if np.shape(getattr(norm[k], f)) == np.shape(getattr(previous[k], f))]
        if diffs:
            out["norm_stats_vs_convert_max_abs_diff"] = round(max(diffs), 6)
    return out


# ------------------------------------------------------------------ training


def _gpu_name() -> str:
    try:
        return subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _dir_gb(p: pathlib.Path) -> float:
    return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e9, 2)


def train(*, spec: Mapping[str, Any], settings: FinetuneSettings, config_name: str, exp_name: str,
          checkpoint_base_dir: str, assets_dir: str, reporter: ProgressReporter) -> dict:
    import jax

    import scripts.train as tr

    config = build_config(spec=spec, settings=settings, config_name=config_name, exp_name=exp_name,
                          checkpoint_base_dir=checkpoint_base_dir, assets_dir=assets_dir)
    metrics: dict[str, Any] = {"gpu": _gpu_name(), "steps": settings.steps, "config_name": config.name,
                               "checkpoint_dir_base": str(config.checkpoint_dir)}

    stats_file = pathlib.Path(assets_dir) / spec["repo_id"] / "norm_stats.json"
    if settings.norm_stats == "recompute" or not stats_file.exists():
        reporter.report(status="running", phase="norm_stats", step=0)
        metrics.update(compute_norm_stats(config, num_workers=min(settings.num_workers, 8)))
        metrics["norm_stats_source"] = "openpi"
    else:
        metrics["norm_stats_source"] = "convert"
        metrics["norm_stats_path"] = str(stats_file)

    # --- hooks: openpi's own log lines (phase timings) and its progress-bar Step lines (loss) ---
    marks: dict[str, float] = {}
    curve = LossCurve()
    last_reported = {"step": -1}
    t_main = time.time()

    class _Marks(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            for key, phase in (("Initialized data loader", "load_weights"), ("Initialized train state", "compile"),
                               ("Waiting for checkpoint manager", "save")):
                if msg.startswith(key) and key not in marks:
                    marks[key] = time.time()
                    reporter.report(status="running", phase=phase, step=curve.last[0] if curve.last else 0)

    logging.basicConfig(level=logging.INFO)
    marks_handler = _Marks()
    logging.getLogger().addHandler(marks_handler)

    # wandb.init(mode="disabled") rebinds wandb.log, so hook the progress bar's write() instead
    # (train.py prints "Step N: grad_norm=..., loss=..., param_norm=..." there every log_interval).
    orig_tqdm = tr.tqdm

    def _on_step_line(text: str) -> None:
        m = _STEP_RE.match(text)
        if not m:
            return
        step = int(m.group(1))
        data = {k: float(v) for k, v in _KV_RE.findall(m.group(2))}
        curve.add(step, data)
        if "loss" in data and (step == 0 or step - last_reported["step"] >= settings.progress_every):
            last_reported["step"] = step
            sps = curve.sec_per_step()
            reporter.report(status="running", phase="train", step=step, loss=round(data["loss"], 6),
                            grad_norm=round(data.get("grad_norm", float("nan")), 6),
                            sec_per_step=round(sps, 3) if sps else None,
                            eta_s=round(sps * (settings.steps - step) + 45) if sps else None,
                            loss_curve=curve.curve(max_points=100))

    class _HookedTqdm(orig_tqdm.tqdm):
        @classmethod
        def write(cls, s, *a, **k):
            try:
                _on_step_line(str(s))
            except Exception:  # noqa: BLE001 - progress must never break training
                pass
            return orig_tqdm.tqdm.write(s, *a, **k)

    tr.tqdm = types.SimpleNamespace(tqdm=_HookedTqdm)
    peak = {"mem": 0}
    stop = threading.Event()

    def _sample():
        while not stop.is_set():
            try:
                r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
                peak["mem"] = max(peak["mem"], int(r.splitlines()[0]))
            except Exception:  # noqa: BLE001
                pass
            stop.wait(5)

    threading.Thread(target=_sample, daemon=True).start()
    try:
        reporter.report(status="running", phase="load_data", step=0)
        tr.main(config)
    finally:
        stop.set()
        tr.tqdm = orig_tqdm
        logging.getLogger().removeHandler(marks_handler)
    t_end = time.time()

    mem = jax.local_devices()[0].memory_stats() or {}
    metrics["train_s"] = round(t_end - t_main, 1)
    metrics["peak_gpu_mem_gb"] = round(mem.get("peak_bytes_in_use", 0) / 1e9, 2)
    metrics["max_nvidia_smi_mem_used_mib"] = peak["mem"]
    if "Initialized data loader" in marks:
        metrics["dataloader_init_s"] = round(marks["Initialized data loader"] - t_main, 1)
    if "Initialized train state" in marks and "Initialized data loader" in marks:
        metrics["weights_load_s"] = round(marks["Initialized train state"] - marks["Initialized data loader"], 1)
    if curve.points:
        if "Initialized train state" in marks:
            metrics["first_step_incl_compile_s"] = round(curve.times[0] - marks["Initialized train state"], 1)
        metrics["checkpoint_save_s"] = round(t_end - curve.times[-1], 1)
        sps = curve.sec_per_step()
        if sps:
            metrics["sec_per_step"] = round(sps, 3)
        metrics["first_loss"] = round(curve.points[0][1]["loss"], 6)
        metrics["final_loss"] = round(curve.points[-1][1]["loss"], 6)
        metrics["final_loss_step"] = curve.points[-1][0]
        metrics["grad_norm_last"] = round(curve.points[-1][1].get("grad_norm", float("nan")), 6)
        metrics["loss_curve"] = curve.curve(max_points=200)
        metrics["grad_norm_curve"] = curve.curve("grad_norm", max_points=200)

    ckpt_dir = pathlib.Path(config.checkpoint_dir)
    steps_saved = sorted((p for p in ckpt_dir.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    if not steps_saved:
        raise RuntimeError(f"training finished but no checkpoint step dir under {ckpt_dir}")
    last = steps_saved[-1]
    for sub in ("params", "assets"):
        if not (last / sub).exists():
            raise RuntimeError(f"checkpoint {last} has no {sub}/")
    metrics["checkpoint_gb"] = _dir_gb(last)
    metrics["checkpoint_steps"] = [p.name for p in steps_saved]
    return {"checkpoint_dir": str(last), "metrics": metrics}


# ------------------------------------------------------------------ loading (TDM-41)


def load_policy(run_json: str | pathlib.Path, *, checkpoint_step_dir: str | None = None):
    """Rebuild the run's config from ``rdf_run.json`` and return openpi's trained policy."""
    import openpi.policies.policy_config as policy_config

    p = pathlib.Path(run_json)
    meta = json.loads((p / "rdf_run.json" if p.is_dir() else p).read_text())
    config = build_config(spec=meta["dataset_spec"], settings=meta["settings"], config_name=meta["config_name"],
                          exp_name=meta["exp_name"], checkpoint_base_dir="/tmp/rdf-unused",
                          assets_dir="/tmp/rdf-unused-assets")
    return policy_config.create_trained_policy(config, checkpoint_step_dir or meta["checkpoint_step_dir"])
