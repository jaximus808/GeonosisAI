"""rdf-training: the production Modal app for E05 (conversion, pi0.5 fine-tune, policy serving).

TDM-38 owns this file: the app, the image, the Volumes, the Secret and the function
signatures. The bodies are STUBS for now (sleep, then return ``stub://`` URIs) so the
orchestrator's Modal client (TDM-21) can integrate before the real work lands:

    convert(ticket_id, episode_manifest) -> dataset_uri                    TDM-39 (rdf_training.convert)
    finetune(ticket_id, dataset_uri, hyperparams) -> {checkpoint_uri, metrics, gpu_seconds}  TDM-40 (rdf_training.finetune)
    Policy (class): infer(obs) -> {"actions": [[...]] }, health()          TDM-41

Calling it from the orchestrator (Modal 1.x; ``Function.lookup`` no longer exists)::

    import modal
    finetune = modal.Function.from_name("rdf-training", "finetune")
    call = finetune.spawn(ticket_id="42", dataset_uri="modal-vol://rdf-datasets/lerobot/rdf/ticket-42",
                          hyperparams={"steps": 2000})
    call.object_id                 # -> store as training_run.modal_call_id
    modal.FunctionCall.from_id(call_id).get(timeout=0)   # poll; raises TimeoutError while running
    Policy = modal.Cls.from_name("rdf-training", "Policy"); Policy().infer.remote(obs)

Module boundary with TDM-39: ``convert`` below calls ``rdf_training.convert.modal_convert(manifest,
datasets_root="/datasets", media_root="/media")`` (TDM-39's file, ``src/rdf_training/convert.py``) and
returns ``modal-vol://rdf-datasets/lerobot/<repo_id>``. With no episodes in the manifest (or no
convert module) it returns a ``stub://`` URI. TDM-39 does not need to edit this file; TDM-40/41
replace their stub bodies (and set GPU sizes).

Modal resources (all created by TDM-38):
    app     rdf-training
    volume  rdf-checkpoints  -> /checkpoints   openpi-cache/ (pi05_base params + tokenizer, seeded from
                                               rdf-s05a-checkpoints by seed_base_weights), runs/, metrics/
    volume  rdf-datasets     -> /datasets      lerobot/<repo_id>/ (HF_LEROBOT_HOME), assets/<repo_id>/ norm stats
    volume  rdf-media        -> /media (ro)    TDM-4's episode media store: tickets/<t>/episodes/<e>/<file>
    secret  rdf-training     HF_TOKEN etc. from .env, via scripts/sync_modal_secret.py (make modal-secrets)

URIs use the TDM-4 scheme ``modal-vol://<volume>/<path>``.

Commands (repo root):
    make modal-secrets                         # create/refresh the rdf-training secret from .env
    make deploy-modal                          # modal deploy services/training/modal_app.py
    cd services/training && uv run modal run modal_app.py::finetune --ticket-id test   # runs, prints nothing
    cd services/training && uv run modal run modal_app.py --action finetune --ticket-id test   # prints the result
    cd services/training && uv run modal run modal_app.py::seed_base_weights   # one-time, CPU only
No function sets min_containers / keep_warm. finetune requests 1x H100 (TDM-40); the stubs request no GPU.
"""

# No `from __future__ import annotations`: modal.parameter() needs real (non-string) annotations.
import json
import pathlib
import time
from typing import Any

import modal

APP_NAME = "rdf-training"
OPENPI_COMMIT = "215abfb217dbac7d5f1273282331b9b1866c0479"  # Physical-Intelligence/openpi main, 2026-08-24 (S-05a)

CHECKPOINTS_VOLUME = "rdf-checkpoints"
DATASETS_VOLUME = "rdf-datasets"
MEDIA_VOLUME = "rdf-media"  # owned by services/media-store (TDM-4); read-only here
SECRET_NAME = "rdf-training"

CHECKPOINTS_ROOT = "/checkpoints"
DATASETS_ROOT = "/datasets"
MEDIA_ROOT = "/media"

STUB_SLEEP_S = 20  # TDM-21 tests against "a stub that sleeps 20 s"

HERE = pathlib.Path(__file__).parent

app = modal.App(APP_NAME)

checkpoints_volume = modal.Volume.from_name(CHECKPOINTS_VOLUME, create_if_missing=True)
datasets_volume = modal.Volume.from_name(DATASETS_VOLUME, create_if_missing=True)
media_volume = modal.Volume.from_name(MEDIA_VOLUME).read_only()
secret = modal.Secret.from_name(SECRET_NAME)

# The S-05a image, step for step (so its expensive layers come from Modal's image cache),
# with production Volume paths and our own source added at the end.
openpi_image = (
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
            "OPENPI_DATA_HOME": f"{CHECKPOINTS_ROOT}/openpi-cache",
            "HF_LEROBOT_HOME": f"{DATASETS_ROOT}/lerobot",
            "HF_HUB_ENABLE_HF_TRANSFER": "0",
            "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.9",
            "TOKENIZERS_PARALLELISM": "false",
            # openpi's scripts/ must be importable by name so spawned DataLoader workers can unpickle.
            "PYTHONPATH": "/openpi:/root",
        }
    )
)


def _with_src(img: modal.Image) -> modal.Image:
    """Our source goes last: Modal rejects build steps after add_local_* (TDM-41 split, same layers)."""
    return img.add_local_dir(HERE / "src" / "rdf_training", "/root/rdf_training", ignore=["**/__pycache__"]).add_local_dir(
        HERE.parent / "common" / "src" / "rdf_common", "/root/rdf_common", ignore=["**/__pycache__"]
    )


image = _with_src(openpi_image)


def vol_uri(volume: str, path: str) -> str:
    return f"modal-vol://{volume}/{path.lstrip('/')}"


def _stub_uri(volume: str, path: str) -> str:
    return f"stub://{volume}/{path.lstrip('/')}"


def _json_arg(value: Any, default: Any) -> Any:
    """Accept a Python object (spawn/remote) or a JSON string (`modal run ... --hyperparams '{...}'`)."""
    if value is None or value == "":
        return default
    return json.loads(value) if isinstance(value, str) else value


# ---------------------------------------------------------------- convert (TDM-39)


@app.function(
    image=image,
    volumes={DATASETS_ROOT: datasets_volume, MEDIA_ROOT: media_volume},
    secrets=[secret],
    cpu=8,
    memory=16384,
    timeout=3600,
)
def convert(ticket_id: str, episode_manifest=None) -> str:
    """Episodes on rdf-media -> LeRobot v2.1 dataset (+ openpi norm stats) on rdf-datasets. Returns dataset_uri.

    episode_manifest: TDM-39's ``ConvertManifest`` as a dict (``repo_id``, ``task``, ``episodes``, ...;
    see ``rdf_training.convert``), or its JSON string from the CLI. ``repo_id`` defaults to
    ``rdf/ticket-<ticket_id>``. The conversion report is printed and also written by TDM-39 to
    ``<dataset>/meta/rdf_conversion.json``.
    """
    manifest = dict(_json_arg(episode_manifest, {}))
    manifest.setdefault("repo_id", f"rdf/ticket-{ticket_id}")
    import importlib.util

    # Only a *missing* module means "not built yet"; a broken one should fail loudly.
    if importlib.util.find_spec("rdf_training.convert") is None or not manifest.get("episodes"):
        time.sleep(5)
        return _stub_uri(DATASETS_VOLUME, f"lerobot/{manifest['repo_id']}")

    from rdf_training.convert import modal_convert  # TDM-39

    media_volume.reload()  # see uploads committed after this container started
    report = modal_convert(manifest, datasets_root=DATASETS_ROOT, media_root=MEDIA_ROOT)
    datasets_volume.commit()
    print(json.dumps({"ticket_id": ticket_id, "convert_report": report}, default=str)[:4000])
    return vol_uri(DATASETS_VOLUME, f"lerobot/{manifest['repo_id']}")


# ---------------------------------------------------------------- finetune (TDM-40)

FINETUNE_TIMEOUT_S = 2 * 3600  # hard cap; rdf_training.finetune refuses step counts that cannot fit
progress_dict = modal.Dict.from_name("rdf-training-progress", create_if_missing=True)


# Unannotated JSON args on purpose: the `modal run` CLI only parses unannotated (Any) or scalar params.
# S-05a resources: 1x H100, 16 CPU (12 loader workers), 96 GB RAM. Override per call with
# finetune.with_options(gpu="A100-80GB"). No min_containers / keep_warm: it scales to zero.
@app.function(
    image=image,
    volumes={CHECKPOINTS_ROOT: checkpoints_volume, DATASETS_ROOT: datasets_volume},
    secrets=[secret],
    gpu="H100",
    cpu=16,
    memory=98304,
    timeout=FINETUNE_TIMEOUT_S,
)
def finetune(ticket_id: str, dataset_uri: str = "", hyperparams=None) -> dict:
    """LoRA fine-tune pi0.5 on dataset_uri (TDM-40; see rdf_training.finetune for the full contract).

    hyperparams: run_settings.hyperparams_json as a dict (or its JSON string from the CLI):
    {lr, steps (2000), batch_size (32), lora_rank (16; 0 = full fine-tune), prompt ('' = dataset task)},
    plus optional base_checkpoint_uri, training_run_id, smoke, norm_stats ('auto' | 'recompute').
    Returns {checkpoint_uri: modal-vol://rdf-checkpoints/runs/ticket-<t>/<run>/<step>, metrics, gpu_seconds}.
    Live progress: modal.Dict "rdf-training-progress"[<modal call id>] (+ POST ORCHESTRATOR_URL/modal/progress).
    """
    from rdf_training.finetune import run_finetune

    return run_finetune(
        ticket_id,
        dataset_uri,
        _json_arg(hyperparams, {}),
        checkpoints_root=CHECKPOINTS_ROOT,
        datasets_root=DATASETS_ROOT,
        timeout_s=FINETUNE_TIMEOUT_S,
        commit=checkpoints_volume.commit,
        reload_datasets=datasets_volume.reload,
        progress_dict=progress_dict,
        call_id=modal.current_function_call_id(),
    )


# ---------------------------------------------------------------- policy server (TDM-41)


# TDM-41: base pi0.5 + fine-tuned checkpoints, hot-swapped by URI. Logic and API docs are in
# src/rdf_training/policy_server.py; this class is only the Modal glue. Resources follow S-05b
# (TDM-37): H100 first (41 ms GPU per chunk), us-west GPU + ingress, scale to zero.
# Warm/cool without redeploying: `make policy-up` / `make policy-down` (scripts/policy_ctl.py).
# NOTE: `modal deploy` of this app restarts a warm Policy container (~1 min); it comes back on
# the last activated checkpoint (persisted in the Dict rdf-policy-state).
POLICY_GPU = ["H100", "A100-40GB", "L40S"]  # fallback list; L40S alone never scheduled in us-west
POLICY_MOUNTS = {CHECKPOINTS_VOLUME: CHECKPOINTS_ROOT, "rdf-s05a-checkpoints": "/s05a"}  # volume -> mount
policy_image = _with_src(openpi_image.pip_install("fastapi[standard]==0.115.12"))  # web server deps
s05a_volume = modal.Volume.from_name("rdf-s05a-checkpoints").read_only()  # S-05a smoke checkpoint
policy_state = modal.Dict.from_name("rdf-policy-state", create_if_missing=True)  # active/previous URI


@app.cls(
    image=policy_image,
    gpu=POLICY_GPU,
    region="us-west",
    routing_region="us-west",  # web ingress; the default (us-east) adds ~70 ms RTT from the Bay Area
    volumes={CHECKPOINTS_ROOT: checkpoints_volume, "/s05a": s05a_volume},
    secrets=[secret],  # POLICY_SERVER_TOKEN (bearer auth for /infer, /ws, /policy/activate)
    cpu=4,
    memory=32768,
    min_containers=0,  # scale to zero; `make policy-up` sets min_containers=1 for robot sessions
    max_containers=1,  # the active checkpoint is container state: exactly one server per app
    scaledown_window=300,
    timeout=3600,  # also the longest a single websocket session can stay open
    startup_timeout=1200,
)
@modal.concurrent(max_inputs=8)  # activate() loads next to live inference
class Policy:
    @modal.enter()
    def load(self) -> None:
        import os
        import threading

        import jax

        from rdf_training import policy_server as ps

        jax.config.update("jax_compilation_cache_dir", f"{CHECKPOINTS_ROOT}/jax-cache")
        jax.config.update("jax_persistent_cache_min_compile_time_secs", 1.0)

        def base_dir() -> str:
            from openpi.shared import download

            base = pathlib.Path(CHECKPOINTS_ROOT) / "openpi-cache/openpi-assets/checkpoints/pi05_base"
            if not (base / "assets" / "droid" / "norm_stats.json").exists():  # seed_base_weights copied params only
                download.maybe_download(f"{ps.BASE_URI}/assets/droid", token="anon")
                checkpoints_volume.commit()
            return str(base)

        def before_load(r) -> None:
            if r.path.startswith(CHECKPOINTS_ROOT):
                try:
                    checkpoints_volume.reload()  # see runs committed after this container started
                except Exception as e:  # noqa: BLE001 - open files block a reload; the path may exist already
                    print(f"[rdf-policy] volume reload skipped: {e}", flush=True)

        def commit_cache() -> None:
            checkpoints_volume.commit()  # persist the JAX compile cache for the next cold start

        self.server = ps.PolicyServer(
            POLICY_MOUNTS,
            ps.openpi_loader(base_dir),
            capacity=int(os.environ.get("POLICY_CACHE_SIZE", "2")),
            persist=lambda st: policy_state.put("state", st),
            before_load=before_load,
            on_loaded=commit_cache,
            info={"gpu": jax.devices()[0].device_kind, "app": APP_NAME},
        )
        try:
            st = policy_state.get("state") or {}
        except Exception:  # noqa: BLE001
            st = {}
        t0 = time.monotonic()
        try:
            self.server.activate(st.get("active") or ps.BASE_URI)
        except Exception as e:  # noqa: BLE001 - a deleted/broken last checkpoint must not brick the server
            print(f"[rdf-policy] could not restore {st.get('active')!r} ({e}); falling back to base", flush=True)
            self.server.activate(ps.BASE_URI)
        self.server.info["startup_s"] = round(time.monotonic() - t0, 1)
        prev = st.get("previous")
        if prev and prev != self.server.active.key:  # refill the A/B cache in the background
            threading.Thread(target=self._preload, args=(prev,), daemon=True).start()

    def _preload(self, uri: str) -> None:
        try:
            self.server.activate(uri, preload_only=True)
        except Exception as e:  # noqa: BLE001
            print(f"[rdf-policy] preload of {uri} failed: {e}", flush=True)

    @modal.method()
    def health(self) -> dict:
        return self.server.health()

    @modal.method()
    def activate(self, checkpoint_uri: str, preload_only: bool = False) -> dict:
        """Hot-swap to checkpoint_uri (Modal-SDK path; the HTTP path is POST /policy/activate)."""
        return self.server.activate(checkpoint_uri, preload_only)

    @modal.method()
    def infer(self, obs: dict) -> dict:
        """S-05b payload ({images, state, prompt}) -> {actions [50, action_dim], ...}. See policy_server.py."""
        return self.server.infer(obs)

    @modal.asgi_app()
    def web(self):
        from rdf_training import policy_server as ps

        return ps.build_app(self.server)


# ---------------------------------------------------------------- one-time setup


@app.function(
    image=image,
    volumes={
        CHECKPOINTS_ROOT: checkpoints_volume,
        "/s05a": modal.Volume.from_name("rdf-s05a-checkpoints").read_only(),
    },
    cpu=4,
    memory=4096,
    timeout=3600,
)
def seed_base_weights() -> dict:
    """Copy the cached pi05_base params + tokenizer from the S-05a spike Volume (avoids a 12 GB re-download)."""
    import shutil

    src = pathlib.Path("/s05a/openpi-cache")
    dst = pathlib.Path(CHECKPOINTS_ROOT) / "openpi-cache"
    dst.mkdir(parents=True, exist_ok=True)
    t = time.time()
    copied = []
    for child in sorted(src.iterdir()):
        target = dst / child.name
        if target.exists():
            continue
        partial = dst / f".{child.name}.partial"
        shutil.rmtree(partial, ignore_errors=True)
        shutil.copytree(child, partial)
        partial.rename(target)
        copied.append(child.name)
    checkpoints_volume.commit()
    size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    return {"copied": copied, "openpi_cache_gb": round(size / 1e9, 2), "seconds": round(time.time() - t, 1)}


# ---------------------------------------------------------------- local smoke entrypoint


@app.local_entrypoint()
def main(action: str = "finetune", ticket_id: str = "test", dataset_uri: str = "", hyperparams: str = ""):
    """`modal run modal_app.py --action finetune|convert|policy --ticket-id test` prints the (stub) result."""
    if action == "convert":
        res = convert.remote(ticket_id=ticket_id, episode_manifest=hyperparams or None)
    elif action == "policy":
        p = Policy()
        res = {"health": p.health.remote(), "infer": p.infer.remote({"observation/state": [0.0] * 7})["actions"][:2]}
    else:
        res = finetune.remote(ticket_id=ticket_id, dataset_uri=dataset_uri, hyperparams=hyperparams or None)
    print(json.dumps(res, indent=2))
