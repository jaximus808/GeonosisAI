"""TDM-37 / spike S-05b: serve the openpi pi0.5 BASE checkpoint behind a Modal endpoint.

Deploy:   modal deploy spikes/tdm-37-latency/modal_policy.py
Warm:     uv run --with modal python spikes/tdm-37-latency/warm.py up     (min_containers=1, costs GPU-hours)
Cool:     uv run --with modal python spikes/tdm-37-latency/warm.py down   (scale to zero; next call cold-starts)

HTTP contract (POST {url}/infer, same JSON over websocket {url}/ws):
  request : {"images": [<b64 jpeg base cam>, <b64 jpeg wrist cam>],
             "state":  [float, ...]  (<= 8 values: joints..., gripper last),
             "prompt": "pick up the cup"}
  response: {"actions": [[float]*8] * action_horizon,
             "action_horizon": 50, "action_dim": 8,
             "timing": {"decode_ms", "policy_ms", "gpu_ms", "server_ms"}}

The model is pi05_base with the DROID input layout (base cam + wrist cam, 7 joints + gripper)
and the base checkpoint's own `droid` norm stats. That is fine for *latency*; the YAM-specific
state/action mapping belongs to the fine-tuned config (TDM-36/TDM-41).

Env knobs at deploy time (read on the laptop when the file is imported):
  RDF_S05B_GPU            default "H100,A100-40GB,L40S" (fallback list; /health reports which one)
  RDF_S05B_REGION         default "us-west" (GPU placement, closest to the Bay Area); "" = anywhere
  RDF_S05B_ROUTING_REGION default "us-west" (web ingress); "" = Modal default (us-east)
  RDF_S05B_MIN_CONTAINERS default 0 (scale to zero). Use warm.py instead of redeploying.
  RDF_S05B_ACTION_HORIZON default 50
"""

# No `from __future__ import annotations`: FastAPI must resolve the WebSocket/Request hints
# that are imported inside web(); string annotations broke the /ws route (HTTP 500 on upgrade).
import os

import modal

APP_NAME = "rdf-s05b-policy"
OPENPI_REV = "215abfb217dbac7d5f1273282331b9b1866c0479"  # openpi main, 2026-08-24
CHECKPOINT = "gs://openpi-assets/checkpoints/pi05_base"
CACHE_DIR = "/cache"

# L40S alone sat unscheduled in us-west for 10+ min on 2026-10-24; a fallback list schedules at once.
# H100 first: 41 ms GPU vs 81 ms on A100-40GB, and its us-west host had ~80 ms less network+proxy time.
GPU = os.environ.get("RDF_S05B_GPU", "H100,A100-40GB,L40S").split(",")  # comma list = fallbacks
REGION = os.environ.get("RDF_S05B_REGION", "us-west") or None
ROUTING_REGION = os.environ.get("RDF_S05B_ROUTING_REGION", "us-west") or None
MIN_CONTAINERS = int(os.environ.get("RDF_S05B_MIN_CONTAINERS", "0"))
ACTION_HORIZON = int(os.environ.get("RDF_S05B_ACTION_HORIZON", "50"))

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git", "git-lfs", "build-essential", "clang", "ffmpeg", "libgl1", "libglib2.0-0")
    .pip_install("uv==0.8.13")
    .run_commands(
        f"GIT_LFS_SKIP_SMUDGE=1 git clone https://github.com/Physical-Intelligence/openpi /openpi"
        f" && cd /openpi && git checkout {OPENPI_REV} && git submodule update --init --recursive",
        # Install the locked dependency set into the system interpreter so Modal's runtime sees it.
        "cd /openpi && uv export --frozen --no-hashes --no-dev --no-emit-workspace > /tmp/req.txt"
        " && GIT_LFS_SKIP_SMUDGE=1 uv pip install --system -r /tmp/req.txt"
        " && uv pip install --system --no-deps -e /openpi -e /openpi/packages/openpi-client",
    )
    .pip_install("fastapi[standard]==0.115.12")
    .env(
        {
            "OPENPI_DATA_HOME": f"{CACHE_DIR}/openpi",
            "XLA_PYTHON_CLIENT_MEM_FRACTION": "0.9",
            "RDF_S05B_ACTION_HORIZON": str(ACTION_HORIZON),
            "RDF_S05B_GPU": ",".join(GPU),
        }
    )
)

cache = modal.Volume.from_name("rdf-s05b-openpi-cache", create_if_missing=True)
app = modal.App(APP_NAME, image=image)


@app.function(volumes={CACHE_DIR: cache}, timeout=3600, cpu=4)
def download_checkpoint() -> str:
    """One-off: pull the 12.4 GB pi05_base checkpoint into the Volume so cold starts skip GCS."""
    from openpi.shared import download

    path = download.maybe_download(CHECKPOINT)
    cache.commit()
    return str(path)


def _make_config():
    from openpi.models import model as _model
    from openpi.models import pi0_config
    from openpi.policies import droid_policy
    from openpi.training import config as _config
    from openpi import transforms as _transforms

    horizon = int(os.environ.get("RDF_S05B_ACTION_HORIZON", "50"))
    return _config.TrainConfig(
        name="rdf_s05b_pi05_base_droid_layout",
        model=pi0_config.Pi0Config(action_horizon=horizon, pi05=True),
        data=_config.SimpleDataConfig(
            assets=_config.AssetsConfig(asset_id="droid"),
            data_transforms=lambda model: _transforms.Group(
                inputs=[droid_policy.DroidInputs(model_type=_model.ModelType.PI05)],
                outputs=[droid_policy.DroidOutputs()],
            ),
            base_config=_config.DataConfig(prompt_from_task=True),
        ),
    )


@app.cls(
    gpu=GPU,
    region=REGION,
    routing_region=ROUTING_REGION,  # web ingress region; default ingress is us-east
    volumes={CACHE_DIR: cache},
    min_containers=MIN_CONTAINERS,
    max_containers=1,  # one arm; also bounds cost of the unauthenticated spike URL
    scaledown_window=300,
    timeout=600,
    startup_timeout=1200,
)
@modal.concurrent(max_inputs=4)
class Policy:
    @modal.enter()
    def load(self):
        import threading
        import time

        import jax
        import numpy as np
        from openpi.policies import policy_config
        from openpi.shared import download

        jax.config.update("jax_compilation_cache_dir", f"{CACHE_DIR}/jax-cache")
        jax.config.update("jax_persistent_cache_min_compile_time_secs", 1.0)

        t0 = time.monotonic()
        ckpt = download.maybe_download(CHECKPOINT)
        self.policy = policy_config.create_trained_policy(_make_config(), ckpt)
        t_load = time.monotonic() - t0

        # Wrap the jitted sampler so we can time GPU work alone (block until the device is done).
        orig = self.policy._sample_actions
        self._gpu_ms = 0.0

        def timed(*args, **kwargs):
            s = time.perf_counter()
            out = jax.block_until_ready(orig(*args, **kwargs))
            self._gpu_ms = (time.perf_counter() - s) * 1000
            return out

        self.policy._sample_actions = timed
        self._lock = threading.Lock()

        # Compile once at startup so the first robot call is not a 60 s JIT.
        t1 = time.monotonic()
        dummy = {
            "observation/exterior_image_1_left": np.zeros((224, 224, 3), np.uint8),
            "observation/wrist_image_left": np.zeros((224, 224, 3), np.uint8),
            "observation/joint_position": np.zeros(7, np.float32),
            "observation/gripper_position": np.zeros(1, np.float32),
            "prompt": "warmup",
        }
        self.policy.infer(dummy)
        self.policy.infer(dummy)
        cache.commit()
        self.startup = {
            "load_s": round(t_load, 1),
            "compile_s": round(time.monotonic() - t1, 1),
            "gpu": os.environ.get("RDF_S05B_GPU"),
            "device": jax.devices()[0].device_kind,
        }
        print("[rdf-s05b] ready", self.startup, flush=True)

    def _infer(self, req: dict) -> dict:
        import base64
        import io
        import time

        import numpy as np
        from PIL import Image

        t0 = time.perf_counter()
        imgs = []
        for b64 in req["images"][:2]:
            im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            if im.size != (224, 224):
                im = im.resize((224, 224))
            imgs.append(np.asarray(im, dtype=np.uint8))
        while len(imgs) < 2:
            imgs.append(np.zeros((224, 224, 3), np.uint8))
        state = np.zeros(8, np.float32)
        s = np.asarray(req.get("state", []), np.float32)[:8]
        if len(s):
            # joints fill the front, gripper (last value) goes to slot 7, matching the DROID layout
            state[: len(s) - 1] = s[:-1]
            state[7] = s[-1]
        obs = {
            "observation/exterior_image_1_left": imgs[0],
            "observation/wrist_image_left": imgs[1],
            "observation/joint_position": state[:7],
            "observation/gripper_position": state[7:8],
            "prompt": req.get("prompt") or "do the task",
        }
        t1 = time.perf_counter()
        with self._lock:
            out = self.policy.infer(obs)
            gpu_ms = self._gpu_ms
        t2 = time.perf_counter()
        actions = np.asarray(out["actions"], np.float32)
        return {
            "actions": actions.round(5).tolist(),
            "action_horizon": int(actions.shape[0]),
            "action_dim": int(actions.shape[1]),
            "timing": {
                "decode_ms": round((t1 - t0) * 1000, 2),
                "policy_ms": round((t2 - t1) * 1000, 2),
                "gpu_ms": round(gpu_ms, 2),
                "server_ms": round((time.perf_counter() - t0) * 1000, 2),
            },
        }

    @modal.method()
    def infer(self, req: dict) -> dict:
        """Modal-client path: modal.Cls.from_name(APP_NAME, "Policy")().infer.remote(req)."""
        return self._infer(req)

    @modal.asgi_app()
    def web(self):
        import asyncio
        import json

        from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect

        api = FastAPI(title="rdf-s05b-policy")
        # Raw body + json.loads instead of a pydantic `dict` body: the 45 KB base64 payload is
        # otherwise validated field by field on every call.

        @api.get("/health")
        def health():
            return {"ok": True, **self.startup}

        @api.post("/infer")
        async def infer(request: Request):
            req = json.loads(await request.body())
            out = await asyncio.to_thread(self._infer, req)
            return Response(json.dumps(out), media_type="application/json")

        @api.websocket("/ws")
        async def ws(sock: WebSocket):
            await sock.accept()
            try:
                while True:
                    req = json.loads(await sock.receive_text())
                    await sock.send_text(json.dumps(await asyncio.to_thread(self._infer, req)))
            except WebSocketDisconnect:
                pass

        return api
