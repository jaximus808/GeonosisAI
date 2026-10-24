"""TDM-40: pi0.5 LoRA fine-tune on a per-ticket LeRobot v2.1 dataset (the S-05a recipe, productionised).

``modal_app.finetune(ticket_id, dataset_uri, hyperparams)`` calls :func:`run_finetune` inside the
openpi image. This module is pure Python (no openpi/jax import at module level) so its logic is unit
tested locally; the openpi half lives in ``rdf_training.finetune_openpi`` (container only).

Contract
--------
Input:
    ticket_id    "42"
    dataset_uri  ``modal-vol://rdf-datasets/lerobot/<repo_id>`` (what TDM-39's ``convert`` returns)
    hyperparams  ``run_settings.hyperparams_json`` (``{lr, steps, batch_size, lora_rank, prompt}``),
                 optionally plus ``base_checkpoint_uri``, ``training_run_id``, ``smoke``, ``norm_stats``
                 ("auto" | "recompute"), ``num_workers``, ``log_interval``, ``progress_every``.
                 ``lora_rank`` 0 means full fine-tune; 16 (default) is openpi's ``gemma_2b_lora``.
Output:
    {"checkpoint_uri": "modal-vol://rdf-checkpoints/runs/ticket-<t>/<run>/<last_step>",
     "metrics": {final_loss, steps, wall_s, loss_curve: [[step, loss], ...], ...},
     "gpu_seconds": float}

Checkpoint layout (identical to S-05a's, which TDM-41 loads with ``create_trained_policy``)::

    rdf-checkpoints:/runs/ticket-<t>/<run>/<last_step>/{params/, assets/<repo_id>/norm_stats.json, train_state/}
    rdf-checkpoints:/runs/ticket-<t>/<run>/rdf_run.json      # how to rebuild the config (spec, hp, prompt)
    rdf-checkpoints:/metrics/ticket-<t>/<run>.json           # full metrics + loss curve

Progress (every ``progress_every`` steps, default 50), best effort, never blocks training:
    * Modal Dict ``rdf-training-progress``: key = the Modal call id (``training_run.modal_call_id``)
      and ``ticket-<t>/latest``. Read with ``modal.Dict.from_name("rdf-training-progress").get(call_id)``.
    * ``POST {ORCHESTRATOR_URL}/modal/progress`` and, at the end, ``POST {ORCHESTRATOR_URL}/modal/callback``
      (``Authorization: Bearer $ORCHESTRATOR_TOKEN`` when set). Skipped when ORCHESTRATOR_URL is blank.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import re
import threading
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

CHECKPOINTS_VOLUME = "rdf-checkpoints"
DATASETS_VOLUME = "rdf-datasets"
PROGRESS_DICT = "rdf-training-progress"
PI05_BASE_PARAMS = "gs://openpi-assets/checkpoints/pi05_base/params"
OPENPI_COMMIT = "215abfb217dbac7d5f1273282331b9b1866c0479"

# S-05a / run_settings defaults (spacetime enums.ts DEFAULT_RUN_SETTINGS).
DEFAULT_HYPERPARAMS: dict[str, Any] = {"lr": 2.5e-5, "steps": 2000, "batch_size": 32, "lora_rank": 16, "prompt": ""}
OPENPI_LORA_RANK = 16  # gemma_2b_lora's rank; the action expert (gemma_300m_lora) uses 2x
ACTION_HORIZON = 50
SMOKE_STEPS_BELOW = 1000  # runs shorter than this are labelled smoke unless told otherwise

# S-05a measurements on H100 (LoRA, bs 32): 1.31 s/step + ~4.5 min fixed overhead.
H100_SEC_PER_STEP = 1.31
FIXED_OVERHEAD_S = 300


class FinetuneError(ValueError):
    pass


# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class FinetuneSettings:
    lr: float
    steps: int
    batch_size: int
    lora_rank: int
    prompt: str
    mode: str  # "lora" | "full"
    action_horizon: int = ACTION_HORIZON
    base_params: str = PI05_BASE_PARAMS
    smoke: bool = False
    norm_stats: str = "auto"  # "auto": reuse <datasets>/assets/<repo_id>/norm_stats.json if present
    num_workers: int = 12
    log_interval: int = 10
    progress_every: int = 50
    training_run_id: int | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _num(hp: Mapping[str, Any], key: str, cast, lo, hi):
    v = hp.get(key)
    if v is None or v == "":
        v = DEFAULT_HYPERPARAMS.get(key)
    try:
        x = cast(v)
    except (TypeError, ValueError):
        raise FinetuneError(f"hyperparams.{key}={v!r} is not a {cast.__name__}") from None
    if isinstance(x, float) and not math.isfinite(x):
        raise FinetuneError(f"hyperparams.{key} must be finite")
    if not (lo <= x <= hi):
        raise FinetuneError(f"hyperparams.{key}={x} out of range [{lo}, {hi}]")
    return x


def base_params_path(base_checkpoint_uri: str | None, *, checkpoints_root: str = "/checkpoints") -> str:
    """Weights to start from. Empty / the pi05_base URI -> openpi's cached base; a
    ``modal-vol://rdf-checkpoints/...`` step dir (an earlier run) -> its ``params/`` on the mount."""
    uri = (base_checkpoint_uri or "").strip()
    if not uri or uri.rstrip("/") in (PI05_BASE_PARAMS, PI05_BASE_PARAMS.rsplit("/", 1)[0], "pi05_base"):
        return PI05_BASE_PARAMS
    prefix = f"modal-vol://{CHECKPOINTS_VOLUME}/"
    if uri.startswith(prefix):
        p = f"{checkpoints_root.rstrip('/')}/{uri[len(prefix):].strip('/')}"
        return p if p.endswith("/params") else f"{p}/params"
    if uri.startswith("gs://") or uri.startswith("/"):
        return uri
    raise FinetuneError(f"unsupported base_checkpoint_uri {uri!r} (want gs://, /abs or {prefix}...)")


def resolve_hyperparams(hyperparams: Mapping[str, Any] | None, *, checkpoints_root: str = "/checkpoints") -> FinetuneSettings:
    """run_settings hyperparams -> validated settings. Unknown keys are kept in ``extra`` (and logged)."""
    hp = dict(hyperparams or {})
    lr = _num(hp, "lr", float, 1e-8, 1.0)
    steps = _num(hp, "steps", int, 1, 100_000)
    batch_size = _num(hp, "batch_size", int, 1, 256)
    lora_rank = _num(hp, "lora_rank", int, 0, 1024)
    mode = "full" if lora_rank == 0 else "lora"
    if "mode" in hp and hp["mode"] not in (None, "", mode):
        raise FinetuneError(f"hyperparams.mode={hp['mode']!r} contradicts lora_rank={lora_rank} (0 = full)")
    prompt = str(hp.get("prompt") or "").strip()
    smoke = bool(hp["smoke"]) if "smoke" in hp else steps < SMOKE_STEPS_BELOW
    norm = str(hp.get("norm_stats") or "auto")
    if norm not in ("auto", "recompute"):
        raise FinetuneError(f"hyperparams.norm_stats={norm!r} (want auto | recompute)")
    run_id = hp.get("training_run_id")
    known = {"lr", "steps", "batch_size", "lora_rank", "mode", "prompt", "smoke", "norm_stats", "action_horizon",
             "base_checkpoint_uri", "num_workers", "log_interval", "progress_every", "training_run_id"}
    return FinetuneSettings(
        lr=lr, steps=steps, batch_size=batch_size, lora_rank=lora_rank, prompt=prompt, mode=mode,
        action_horizon=_num({"action_horizon": hp.get("action_horizon") or ACTION_HORIZON}, "action_horizon", int, 1, 200),
        base_params=base_params_path(hp.get("base_checkpoint_uri"), checkpoints_root=checkpoints_root),
        smoke=smoke, norm_stats=norm,
        num_workers=_num({"num_workers": hp.get("num_workers") or 12}, "num_workers", int, 0, 64),
        log_interval=_num({"log_interval": hp.get("log_interval") or 10}, "log_interval", int, 1, 10_000),
        progress_every=_num({"progress_every": hp.get("progress_every") or 50}, "progress_every", int, 1, 10_000),
        training_run_id=int(run_id) if run_id not in (None, "") else None,
        extra={k: v for k, v in hp.items() if k not in known},
    )


def estimate_wall_s(steps: int) -> float:
    return FIXED_OVERHEAD_S + steps * H100_SEC_PER_STEP


def check_fits_timeout(steps: int, timeout_s: float, *, margin_s: float = 300) -> None:
    """Fail before paying for a GPU when the run cannot finish inside the function's hard timeout."""
    est = estimate_wall_s(steps)
    if est + margin_s > timeout_s:
        raise FinetuneError(f"{steps} steps need ~{est / 60:.0f} min on H100; the function timeout is "
                            f"{timeout_s / 60:.0f} min. Lower hyperparams.steps.")


# --------------------------------------------------------------------------- dataset

_URI_RE = re.compile(rf"^modal-vol://{DATASETS_VOLUME}/lerobot/(?P<repo>[^/].*[^/])/?$")


def repo_id_from_dataset_uri(dataset_uri: str) -> str:
    uri = (dataset_uri or "").strip()
    if uri.startswith("stub://"):
        raise FinetuneError(f"dataset_uri {uri!r} is a stub (convert had no episodes); nothing to train on")
    m = _URI_RE.match(uri)
    if not m:
        raise FinetuneError(f"dataset_uri {uri!r}: want modal-vol://{DATASETS_VOLUME}/lerobot/<repo_id>")
    repo = m.group("repo")
    if ".." in repo.split("/"):
        raise FinetuneError("dataset_uri must not contain '..'")
    return repo


def spec_from_info(info: Mapping[str, Any], repo_id: str) -> dict:
    """LeRobot ``meta/info.json`` -> ``openpi_config.DatasetSpec`` fields (the YAM state/action mapping).

    State and action are ``[joints..., gripper]`` (gripper last, absolute); every dim but the last is a
    joint trained as a delta. Base camera = ``observation.images.top``, wrist = ``observation.images.wrist``.
    """
    if info.get("codebase_version") != "v2.1":
        raise FinetuneError(f"{repo_id}: LeRobot codebase_version {info.get('codebase_version')!r}; openpi "
                            f"{OPENPI_COMMIT[:7]} reads v2.1 only")
    feats = info.get("features") or {}
    for k in ("observation.state", "action", "observation.images.top", "observation.images.wrist"):
        if k not in feats:
            raise FinetuneError(f"{repo_id}: feature {k!r} missing (have {sorted(feats)})")
    state_dim = int(feats["observation.state"]["shape"][0])
    action_dim = int(feats["action"]["shape"][0])
    if state_dim != action_dim:
        raise FinetuneError(f"{repo_id}: state dim {state_dim} != action dim {action_dim}")
    if action_dim < 2:
        raise FinetuneError(f"{repo_id}: action dim {action_dim}; need joints + gripper")
    return {
        "repo_id": repo_id,
        "base_image_key": "observation.images.top",
        "wrist_image_key": "observation.images.wrist",
        "state_key": "observation.state",
        "action_key": "action",
        "joint_dims": action_dim - 1,
        "action_dim": action_dim,
        "fps": int(round(float(info.get("fps") or 30))),
    }


# --------------------------------------------------------------------------- run identity / paths


def make_run_id(*, smoke: bool, now: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    return f"{'smoke-' if smoke else ''}{stamp}-{uuid.uuid4().hex[:6]}"


def ticket_dir_name(ticket_id: str) -> str:
    t = str(ticket_id).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", t):
        raise FinetuneError(f"ticket_id {ticket_id!r} must be [A-Za-z0-9._-]+")
    return f"ticket-{t}"


def run_rel_dir(ticket_id: str, run_id: str) -> str:
    """Path inside rdf-checkpoints: ``runs/ticket-<t>/<run>``. openpi's checkpoint_dir is
    ``<checkpoint_base_dir>/<config.name>/<exp_name>``, so config.name = ticket dir, exp_name = run id."""
    return f"runs/{ticket_dir_name(ticket_id)}/{run_id}"


def vol_uri(volume: str, path: str) -> str:
    return f"modal-vol://{volume}/{path.lstrip('/')}"


# --------------------------------------------------------------------------- loss curve


class LossCurve:
    """Collects openpi's per-log_interval means ({loss, grad_norm, param_norm}) keyed by step."""

    def __init__(self) -> None:
        self.points: list[tuple[int, dict[str, float]]] = []
        self.times: list[float] = []

    def add(self, step: int, info: Mapping[str, Any], t: float | None = None) -> None:
        vals = {}
        for k, v in info.items():
            try:
                vals[k] = float(v)
            except (TypeError, ValueError):
                continue
        if "loss" not in vals:
            return
        self.points.append((int(step), vals))
        self.times.append(time.time() if t is None else t)

    @property
    def last(self) -> tuple[int, dict[str, float]] | None:
        return self.points[-1] if self.points else None

    def curve(self, key: str = "loss", max_points: int = 200) -> list[list[float]]:
        pts = [[s, round(v[key], 6)] for s, v in self.points if key in v]
        if len(pts) <= max_points:
            return pts
        stride = math.ceil(len(pts) / max_points)
        out = pts[::stride]
        if out[-1] != pts[-1]:
            out.append(pts[-1])
        return out

    def sec_per_step(self, skip_below: int = 20) -> float | None:
        steady = [(t, s) for t, (s, _v) in zip(self.times, self.points) if s >= skip_below] or list(
            zip(self.times, (s for s, _ in self.points)))
        if len(steady) < 2 or steady[-1][1] == steady[0][1]:
            return None
        return (steady[-1][0] - steady[0][0]) / (steady[-1][1] - steady[0][1])


# --------------------------------------------------------------------------- progress sinks


def _post_json(url: str, payload: Mapping[str, Any], token: str | None, timeout: float = 5.0) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - our own orchestrator URL
        return r.status


class ProgressReporter:
    """Fan progress out to a Modal Dict and the orchestrator. Every sink is best effort and runs off the
    training thread; failures are counted, never raised."""

    def __init__(self, *, base: Mapping[str, Any], keys: list[str], dict_put: Callable[[str, Any], None] | None,
                 orchestrator_url: str | None = None, token: str | None = None,
                 post: Callable[[str, Mapping[str, Any], str | None], int] = _post_json) -> None:
        self.base = dict(base)
        self.keys = keys
        self.dict_put = dict_put
        self.url = (orchestrator_url or "").rstrip("/") or None
        self.token = token or None
        self._post = post
        self.errors: dict[str, int] = {}
        self.sent = 0
        self._threads: list[threading.Thread] = []

    def _err(self, sink: str) -> None:
        self.errors[sink] = self.errors.get(sink, 0) + 1

    def _send(self, payload: dict, path: str | None, to_dict: bool = True) -> None:
        if self.dict_put and to_dict:
            for k in self.keys:
                try:
                    self.dict_put(k, payload)
                except Exception:  # noqa: BLE001
                    self._err("dict")
        if self.url and path:
            try:
                self._post(f"{self.url}{path}", payload, self.token)
            except Exception:  # noqa: BLE001
                self._err(path)
        self.sent += 1

    def report(self, *, wait: bool = False, path: str | None = "/modal/progress", to_dict: bool = True,
               **fields: Any) -> dict:
        payload = {**self.base, **fields, "updated_at": time.time()}
        if wait:
            self._send(payload, path, to_dict)
        else:
            self._threads = [t for t in self._threads if t.is_alive()]
            th = threading.Thread(target=self._send, args=(payload, path, to_dict), daemon=True)
            th.start()
            self._threads.append(th)
        return payload

    def flush(self, timeout: float = 10.0) -> None:
        for th in self._threads:
            th.join(timeout)


# --------------------------------------------------------------------------- the run


def run_finetune(
    ticket_id: str,
    dataset_uri: str,
    hyperparams: Mapping[str, Any] | None,
    *,
    checkpoints_root: str = "/checkpoints",
    datasets_root: str = "/datasets",
    timeout_s: float = 2 * 3600,
    commit: Callable[[], None] = lambda: None,
    reload_datasets: Callable[[], None] = lambda: None,
    progress_dict: Any = None,
    call_id: str | None = None,
) -> dict:
    """Validate, build the openpi config, (re)compute norm stats, train, write the checkpoint + metrics."""
    started = time.time()
    s = resolve_hyperparams(hyperparams, checkpoints_root=checkpoints_root)
    repo_id = repo_id_from_dataset_uri(dataset_uri)
    check_fits_timeout(s.steps, timeout_s)
    run_id = make_run_id(smoke=s.smoke, now=started)
    rel = run_rel_dir(ticket_id, run_id)
    ticket_dir = ticket_dir_name(ticket_id)

    reporter = ProgressReporter(
        base={"kind": "finetune", "ticket_id": str(ticket_id), "training_run_id": s.training_run_id,
              "modal_call_id": call_id, "run_id": run_id, "smoke": s.smoke, "total_steps": s.steps},
        keys=[k for k in (call_id, f"{ticket_dir}/latest", f"{ticket_dir}/{run_id}") if k],
        dict_put=(lambda k, v: progress_dict.put(k, v)) if progress_dict is not None else None,
        orchestrator_url=os.environ.get("ORCHESTRATOR_URL"), token=os.environ.get("ORCHESTRATOR_TOKEN"),
    )
    reporter.report(status="starting", phase="validate", step=0, wait=True)

    metrics: dict[str, Any] = {"ticket_id": str(ticket_id), "run_id": run_id, "dataset_uri": dataset_uri,
                               "repo_id": repo_id, "smoke": s.smoke, "settings": s.as_dict(),
                               "openpi_commit": OPENPI_COMMIT, "estimate_wall_s": round(estimate_wall_s(s.steps))}
    try:
        reload_datasets()
        info_path = pathlib.Path(datasets_root) / "lerobot" / repo_id / "meta" / "info.json"
        if not info_path.exists():
            raise FinetuneError(f"dataset {repo_id} not found at {info_path} (run convert first)")
        info = json.loads(info_path.read_text())
        spec = spec_from_info(info, repo_id)
        metrics["dataset"] = {"episodes": info.get("total_episodes"), "frames": info.get("total_frames"),
                              "fps": info.get("fps"), "spec": spec}

        from rdf_training import finetune_openpi as fo  # container only (openpi, jax)

        out = fo.train(
            spec=spec, settings=s, config_name=ticket_dir, exp_name=run_id,
            checkpoint_base_dir=f"{checkpoints_root.rstrip('/')}/runs",
            assets_dir=f"{datasets_root.rstrip('/')}/assets", reporter=reporter,
        )
        metrics.update(out["metrics"])
        step_dir = pathlib.Path(out["checkpoint_dir"])
        ckpt_rel = f"{rel}/{step_dir.name}"
        checkpoint_uri = vol_uri(CHECKPOINTS_VOLUME, ckpt_rel)
        wall_s = round(time.time() - started, 1)
        metrics.update({"checkpoint_uri": checkpoint_uri, "wall_s": wall_s,
                        "within_estimate": wall_s <= 1.15 * estimate_wall_s(s.steps)})
        run_meta = {
            "ticket_id": str(ticket_id), "run_id": run_id, "checkpoint_uri": checkpoint_uri,
            "checkpoint_step_dir": f"{checkpoints_root.rstrip('/')}/{ckpt_rel}", "dataset_uri": dataset_uri,
            "dataset_spec": spec, "settings": s.as_dict(), "config_name": ticket_dir, "exp_name": run_id,
            "openpi_commit": OPENPI_COMMIT,
            "how_to_load": "rdf_training.finetune_openpi.load_policy(run_json_path) (or openpi_config.make_train_config("
                           "mode, yam spec) + policy_config.create_trained_policy(config, checkpoint_step_dir)); "
                           "lora_rank != 16 needs finetune_openpi.apply_lora_rank(rank) first",
            "created_at": time.time(),
        }
        (pathlib.Path(checkpoints_root) / rel / "rdf_run.json").write_text(json.dumps(run_meta, indent=2))
        # TDM-41's optional sidecar (policy_server.infer_spec): spares it guessing dims/mode from the files.
        (step_dir / "rdf_policy.json").write_text(json.dumps(policy_sidecar(spec, s), indent=2))
        _write_metrics(checkpoints_root, ticket_dir, run_id, metrics)
        commit()
        result = {"checkpoint_uri": checkpoint_uri, "metrics": _summary(metrics), "gpu_seconds": wall_s}
        reporter.report(status="done", phase="done", step=s.steps, loss=metrics.get("final_loss"),
                        loss_curve=metrics.get("loss_curve"), checkpoint_uri=checkpoint_uri, wait=True, path=None)
        reporter.report(status="done", result=result, wait=True, path="/modal/callback", to_dict=False)
        reporter.flush()
        return result
    except BaseException as e:
        metrics.update({"error": f"{type(e).__name__}: {str(e)[:2000]}", "wall_s": round(time.time() - started, 1)})
        try:
            _write_metrics(checkpoints_root, ticket_dir, run_id, metrics)
            commit()
        except Exception:  # noqa: BLE001
            pass
        reporter.report(status="failed", phase="failed", error=metrics["error"], wait=True, path="/modal/progress")
        reporter.report(status="failed", result={"metrics": {"error": metrics["error"]}}, wait=True,
                        path="/modal/callback", to_dict=False)
        raise


def policy_sidecar(spec: Mapping[str, Any], s: FinetuneSettings) -> dict:
    """``<step dir>/rdf_policy.json`` in TDM-41's format, plus the training prompt and LoRA rank."""
    return {"layout": "single_arm", "state_dim": spec["action_dim"], "action_dim": spec["action_dim"],
            "joint_dims": spec["joint_dims"], "asset_id": spec["repo_id"], "mode": s.mode,
            "action_horizon": s.action_horizon, "lora_rank": s.lora_rank, "prompt": s.prompt, "fps": spec["fps"],
            "trained_by": "rdf_training.finetune (TDM-40)"}


def _write_metrics(checkpoints_root: str, ticket_dir: str, run_id: str, metrics: Mapping[str, Any]) -> None:
    d = pathlib.Path(checkpoints_root) / "metrics" / ticket_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps(metrics, indent=2, default=str))


SUMMARY_KEYS = ("final_loss", "first_loss", "steps", "wall_s", "loss_curve", "grad_norm_last", "sec_per_step",
                "train_s", "dataloader_init_s", "weights_load_s", "first_step_incl_compile_s", "checkpoint_save_s",
                "norm_stats_s", "norm_stats_source", "norm_stats_vs_convert_max_abs_diff", "peak_gpu_mem_gb", "gpu",
                "checkpoint_gb", "smoke", "run_id", "repo_id", "dataset", "settings", "estimate_wall_s",
                "within_estimate", "openpi_commit", "checkpoint_uri")


def _summary(metrics: Mapping[str, Any]) -> dict:
    """What goes into training_run.metrics_json (the full set is in rdf-checkpoints:/metrics/)."""
    return {k: metrics[k] for k in SUMMARY_KEYS if k in metrics}
