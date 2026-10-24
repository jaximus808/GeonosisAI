"""rdf-training: the production Modal app for E05 (conversion, pi0.5 fine-tune, policy serving).

TDM-38 owns this file: the app, the image, the Volumes, the Secret and the function
signatures. The bodies are STUBS for now (sleep, then return ``stub://`` URIs) so the
orchestrator's Modal client (TDM-21) can integrate before the real work lands:

    convert(ticket_id, episode_manifest) -> dataset_uri                    TDM-39 (rdf_training.convert)
    finetune(ticket_id, dataset_uri, hyperparams) -> {checkpoint_uri, metrics}   TDM-40
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
No function sets min_containers / keep_warm, and the stubs request no GPU.
"""

# No `from __future__ import annotations`: modal.parameter() needs real (non-string) annotations.
import json
import pathlib
import time
import uuid
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
            "OPENPI_DATA_HOME": f"{CHECKPOINTS_ROOT}/openpi-cache",
            "HF_LEROBOT_HOME": f"{DATASETS_ROOT}/lerobot",
            "HF_HUB_ENABLE_HF_TRANSFER": "0",
            "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.9",
            "TOKENIZERS_PARALLELISM": "false",
            # openpi's scripts/ must be importable by name so spawned DataLoader workers can unpickle.
            "PYTHONPATH": "/openpi:/root",
        }
    )
    .add_local_dir(HERE / "src" / "rdf_training", "/root/rdf_training", ignore=["**/__pycache__"])
    .add_local_dir(HERE.parent / "common" / "src" / "rdf_common", "/root/rdf_common", ignore=["**/__pycache__"])
)


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


# Unannotated JSON args on purpose: the `modal run` CLI only parses unannotated (Any) or scalar params.
# TDM-40: real resources are gpu="H100", cpu=16, memory=98304, timeout=2*3600 (S-05a).
@app.function(
    image=image,
    volumes={CHECKPOINTS_ROOT: checkpoints_volume, DATASETS_ROOT: datasets_volume},
    secrets=[secret],
    cpu=1.0,
    memory=2048,
    timeout=2 * 3600,
)
def finetune(ticket_id: str, dataset_uri: str = "", hyperparams=None) -> dict:
    """LoRA fine-tune pi0.5 on dataset_uri. Returns {checkpoint_uri, metrics}. STUB.

    hyperparams: dict (or its JSON string from the CLI), merged over the S-05a defaults.
    """
    hp = {"mode": "lora", "steps": 2000, "batch_size": 32, "action_horizon": 50, **_json_arg(hyperparams, {})}
    started = time.time()
    time.sleep(float(hp.get("stub_sleep_s", STUB_SLEEP_S)))
    run = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    return {
        "checkpoint_uri": _stub_uri(CHECKPOINTS_VOLUME, f"runs/ticket-{ticket_id}/{run}/{hp['steps'] - 1}"),
        "metrics": {
            "stub": True,
            "ticket_id": ticket_id,
            "dataset_uri": dataset_uri,
            "hyperparams": hp,
            "wall_s": round(time.time() - started, 1),
            "gpu": None,
        },
    }


# ---------------------------------------------------------------- policy server (TDM-41)


# TDM-41: real resources come from S-05b (TDM-37); S-05a measured L40S at 0.12 s per chunk.
@app.cls(
    image=image,
    volumes={CHECKPOINTS_ROOT: checkpoints_volume},
    secrets=[secret],
    cpu=1.0,
    memory=2048,
    scaledown_window=120,
    timeout=1800,
)
class Policy:
    checkpoint_uri: str = modal.parameter(default="")

    @modal.enter()
    def load(self) -> None:
        self.loaded_at = time.time()  # TDM-41: create_trained_policy(config, ckpt_dir) here

    @modal.method()
    def health(self) -> dict:
        return {"ok": True, "stub": True, "checkpoint_uri": self.checkpoint_uri, "loaded_at": self.loaded_at}

    @modal.method()
    def infer(self, obs: dict) -> dict:
        """obs: observation/image, observation/wrist_image, observation/state, prompt (S-05a keys).

        STUB: returns a hold-still chunk (the current state repeated) of shape [50, len(state)].
        """
        state = [float(x) for x in (obs.get("observation/state") or [0.0] * 7)]
        return {"actions": [list(state) for _ in range(50)], "stub": True, "checkpoint_uri": self.checkpoint_uri}


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
