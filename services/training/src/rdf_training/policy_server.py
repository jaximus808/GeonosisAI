"""TDM-41: production pi0.5 policy server (base + fine-tuned checkpoints, hot-swapped by URI).

Promoted from the S-05b spike (``spikes/tdm-37-latency/modal_policy.py``). The Modal class that
hosts it is ``Policy`` in ``services/training/modal_app.py`` (app ``rdf-training``). Everything that
is not Modal glue lives here, so it can be unit-tested on the laptop without openpi
(openpi, jax, PIL and FastAPI are imported lazily, inside the container).

HTTP / websocket API (base URL = ``Policy.web``, e.g. ``https://jaxonp808--rdf-training-policy-web.us-west.modal.run``)
  GET  /health                   public. {ok, ready, active_checkpoint_uri, active, cached, loading, gpu, ...}
  POST /infer                    bearer. S-05b payload -> one action chunk
  WS   /ws                       bearer (header, or ?token= for clients that cannot set headers).
                                 Same JSON as /infer, one message per call. Errors come back as
                                 {"error", "code"} and the socket stays open.
  POST /policy/activate          bearer. {"checkpoint_uri", "preload_only"?: bool} -> hot-swap
       (alias /activate)         (the orchestrator's eval step and the robot's load_checkpoint use this)
  GET  /policy                   bearer. Same as /health (kept for symmetry with /policy/activate)

Request (S-05b):  {"images": [<b64 JPEG base cam>, <b64 JPEG wrist cam>], "state": [floats], "prompt": str,
                   "checkpoint_uri"?: str}
    ``checkpoint_uri`` is optional. When given it must be the active checkpoint, otherwise the call
    fails with code ``checkpoint_mismatch`` (so a robot never executes another policy's actions).
Response:          {"actions": [[float]*action_dim]*50, "action_horizon", "action_dim", "action_space",
                    "checkpoint_uri", "state_dim", "timing": {decode_ms, policy_ms, gpu_ms, server_ms}}

Robot state/action mapping (decided per checkpoint, see ``PolicySpec``):
  * Fine-tuned checkpoints (layout ``single_arm``): exactly the training mapping of
    ``rdf_training.openpi_config`` (S-05a). For the YAM: ``state`` = 6 joint positions + gripper
    (7 floats, gripper last). The model is trained on joint DELTAS with an absolute gripper; the
    server applies openpi's AbsoluteActions output transform, so ``actions`` come back as ABSOLUTE
    joint targets [50, 7] (``action_space = "joint_pos_abs"``). The state length is checked
    against the checkpoint's norm stats; a mismatch is an error, never silently padded.
  * Base pi0.5 (layout ``droid``): the DROID input layout with the base checkpoint's ``droid``
    norm stats, as in S-05b. ``state`` <= 8 floats, gripper last, zero-padded. Output is 8-dim
    (``action_space = "droid_base"``). It only means something for baseline evals.

Checkpoint URIs:
  base                      ``gs://openpi-assets/checkpoints/pi05_base`` (``/params`` suffix, ``base``,
                            ``pi05_base`` are accepted too)
  fine-tuned                ``modal-vol://<volume>/<path to the step dir>`` (or ``<volume>:/<path>``), e.g.
                            ``modal-vol://rdf-checkpoints/runs/ticket-42/<run>/1999`` or the S-05a smoke
                            ``modal-vol://rdf-s05a-checkpoints/checkpoints/rdf_s05a_pi05_lora/smoke_lora_h100/199``.
                            A trailing ``/params`` is stripped. ``stub://`` URIs (TDM-38 stubs) are refused.
  Optional sidecar          ``<step dir>/rdf_policy.json`` {layout, state_dim, action_dim, joint_dims, asset_id,
                            mode, action_horizon}. Without it the spec is inferred from the checkpoint:
                            ``assets/<asset_id>/norm_stats.json`` gives the dims, ``params/_METADATA``
                            containing ``lora_a`` gives mode=lora, ``joint_dims = action_dim - 1``.

Hot-swap: one container (``max_containers=1``) keeps an LRU cache of ``POLICY_CACHE_SIZE`` (default 2)
loaded policies on the GPU. Activating a cached checkpoint is a pointer swap (milliseconds), so the
live A/B can flip back and forth. Activating an uncached one loads it next to the active policy
while inference keeps running on the old one, then swaps. ``preload_only`` loads without
switching. The active and previous URIs are persisted in the Modal Dict ``rdf-policy-state``, so a
container that scaled to zero comes back on the same checkpoint.
"""

# No `from __future__ import annotations`: FastAPI must resolve the Request/WebSocket hints in
# build_app(), which are imported locally; string annotations turn them into query params (422).
import collections
import dataclasses
import hmac
import json
import os
import pathlib
import threading
import time
from typing import Any, Callable

BASE_URI = "gs://openpi-assets/checkpoints/pi05_base"
BASE_ALIASES = {"base", "pi05_base", BASE_URI, BASE_URI + "/", BASE_URI + "/params"}
TOKEN_ENV = "POLICY_SERVER_TOKEN"
STATE_DICT_NAME = "rdf-policy-state"
DEFAULT_ACTION_HORIZON = 50
IMAGE_SIZE = 224


class PolicyError(Exception):
    """A client error (bad URI, bad payload, mismatch). ``code`` is machine-readable; HTTP status 4xx."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status

    def __reduce__(self):  # survives the trip back through a Modal .remote() call
        return (PolicyError, (self.code, str(self), self.status))


# ---------------------------------------------------------------- checkpoint URIs


@dataclasses.dataclass(frozen=True)
class ResolvedCheckpoint:
    key: str  # canonical URI (cache key)
    kind: str  # "base" | "finetuned"
    path: str  # local path inside the container ("" for base: resolved by the loader)


def resolve_uri(uri: str, mounts: dict[str, str]) -> ResolvedCheckpoint:
    """Map a checkpoint URI to a canonical key and a container path. ``mounts``: volume name -> mount path."""
    u = (uri or "").strip()
    if not u or u in BASE_ALIASES:
        return ResolvedCheckpoint(BASE_URI, "base", "")
    if u.startswith("stub://"):
        raise PolicyError("stub_checkpoint", f"{u} is a stub URI (TDM-38 stub finetune); there is no checkpoint to load")
    if u.startswith("modal-vol://"):
        vol, _, rest = u[len("modal-vol://"):].partition("/")
    elif ":/" in u and "://" not in u:
        vol, _, rest = u.partition(":/")
    else:
        raise PolicyError("bad_uri", f"unsupported checkpoint URI {u!r}: use {BASE_URI} or modal-vol://<volume>/<path>")
    parts = [p for p in rest.strip("/").split("/") if p]
    if parts and parts[-1] == "params":
        parts = parts[:-1]
    if not parts or any(p in (".", "..") for p in parts):
        raise PolicyError("bad_uri", f"bad checkpoint path in {u!r}")
    if vol not in mounts:
        raise PolicyError("unknown_volume", f"volume {vol!r} is not mounted on the policy server (have {sorted(mounts)})")
    rel = "/".join(parts)
    return ResolvedCheckpoint(f"modal-vol://{vol}/{rel}", "finetuned", f"{mounts[vol].rstrip('/')}/{rel}")


# ---------------------------------------------------------------- per-checkpoint state/action mapping


@dataclasses.dataclass(frozen=True)
class PolicySpec:
    layout: str  # "single_arm" (fine-tuned, S-05a mapping) | "droid" (base pi0.5, baseline only)
    state_dim: int
    action_dim: int
    joint_dims: int  # leading action dims trained as deltas; the rest (gripper) absolute
    asset_id: str  # norm-stats id under <ckpt>/assets/
    mode: str = "lora"  # "lora" | "full"
    action_horizon: int = DEFAULT_ACTION_HORIZON

    @property
    def action_space(self) -> str:
        return "joint_pos_abs" if self.layout == "single_arm" else "droid_base"


# The YAM mapping every real fine-tuned checkpoint should end up with (S-05a: 6 joints + gripper).
YAM_SPEC = PolicySpec(layout="single_arm", state_dim=7, action_dim=7, joint_dims=6, asset_id="")
BASE_SPEC = PolicySpec(layout="droid", state_dim=8, action_dim=8, joint_dims=7, asset_id="droid", mode="base")


def infer_spec(ckpt_dir: str | pathlib.Path) -> PolicySpec:
    """Read ``rdf_policy.json`` if present, else derive the spec from norm stats + param metadata."""
    d = pathlib.Path(ckpt_dir)
    if not (d / "params").is_dir():
        raise PolicyError("not_found", f"no params/ under {d} (is the URI the step directory?)", status=404)
    side = d / "rdf_policy.json"
    meta: dict[str, Any] = json.loads(side.read_text()) if side.exists() else {}
    asset_id = meta.get("asset_id")
    if not asset_id:
        stats = sorted((d / "assets").rglob("norm_stats.json")) if (d / "assets").is_dir() else []
        if len(stats) != 1:
            raise PolicyError("no_norm_stats", f"expected one assets/**/norm_stats.json under {d}, found {len(stats)}")
        asset_id = str(stats[0].parent.relative_to(d / "assets"))
    ns_path = d / "assets" / asset_id / "norm_stats.json"
    ns = json.loads(ns_path.read_text())["norm_stats"] if ns_path.exists() else {}
    state_dim = int(meta.get("state_dim") or len(ns.get("state", {}).get("mean", [])))
    action_dim = int(meta.get("action_dim") or len(ns.get("actions", {}).get("mean", [])))
    if not state_dim or not action_dim:
        raise PolicyError("no_norm_stats", f"cannot read state/action dims from {ns_path}")
    mode = meta.get("mode")
    if not mode:
        md = d / "params" / "_METADATA"
        mode = "lora" if md.exists() and "lora_a" in md.read_text(errors="ignore") else "full"
    return PolicySpec(
        layout=meta.get("layout", "single_arm"),
        state_dim=state_dim,
        action_dim=action_dim,
        joint_dims=int(meta.get("joint_dims", action_dim - 1)),
        asset_id=asset_id,
        mode=mode,
        action_horizon=int(meta.get("action_horizon", DEFAULT_ACTION_HORIZON)),
    )


def make_openpi_config(spec: PolicySpec):
    """openpi TrainConfig used only for inference (create_trained_policy). Container only."""
    from openpi import transforms as _transforms
    from openpi.models import model as _model
    from openpi.models import pi0_config
    from openpi.policies import droid_policy
    from openpi.training import config as _config

    if spec.layout == "droid":
        return _config.TrainConfig(
            name="rdf_pi05_base_droid_layout",
            model=pi0_config.Pi0Config(action_horizon=spec.action_horizon, pi05=True),
            data=_config.SimpleDataConfig(
                assets=_config.AssetsConfig(asset_id="droid"),
                data_transforms=lambda model: _transforms.Group(
                    inputs=[droid_policy.DroidInputs(model_type=_model.ModelType.PI05)],
                    outputs=[droid_policy.DroidOutputs()],
                ),
                base_config=_config.DataConfig(prompt_from_task=True),
            ),
        )
    # Fine-tuned: the exact S-05a training mapping (delta joints in, absolute joints out).
    from rdf_training import openpi_config as oc

    ds = oc.DatasetSpec(
        repo_id=spec.asset_id,
        base_image_key="observation.images.top",
        wrist_image_key="observation.images.wrist",
        joint_dims=spec.joint_dims,
        action_dim=spec.action_dim,
    )
    return oc.make_train_config(mode=spec.mode, spec=ds, action_horizon=spec.action_horizon)


# ---------------------------------------------------------------- observations


def _decode_image(b64: str):
    import base64
    import io

    import numpy as np
    from PIL import Image

    im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    if im.size != (IMAGE_SIZE, IMAGE_SIZE):
        im = im.resize((IMAGE_SIZE, IMAGE_SIZE))
    return np.asarray(im, dtype=np.uint8)


def build_obs(req: dict, spec: PolicySpec, decode: Callable[[str], Any] = _decode_image) -> dict:
    """S-05b request -> openpi observation dict for this checkpoint's layout."""
    import numpy as np

    imgs = [decode(b) for b in (req.get("images") or [])[:2]]
    while len(imgs) < 2:
        imgs.append(np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), np.uint8))
    raw = req.get("state")
    if raw is None:
        raw = req.get("observation/state") or []
    s = np.asarray(raw, np.float32).reshape(-1)
    prompt = req.get("prompt") or "do the task"
    if spec.layout == "droid":
        if len(s) > 8:
            raise PolicyError("bad_state", f"base pi0.5 takes <= 8 state values (joints..., gripper last), got {len(s)}")
        state = np.zeros(8, np.float32)
        if len(s):
            state[: len(s) - 1] = s[:-1]
            state[7] = s[-1]
        return {
            "observation/exterior_image_1_left": imgs[0],
            "observation/wrist_image_left": imgs[1],
            "observation/joint_position": state[:7],
            "observation/gripper_position": state[7:8],
            "prompt": prompt,
        }
    if len(s) != spec.state_dim:
        raise PolicyError(
            "bad_state",
            f"this checkpoint expects state of length {spec.state_dim} "
            f"({spec.joint_dims} joints + gripper last), got {len(s)}",
        )
    return {"observation/image": imgs[0], "observation/wrist_image": imgs[1], "observation/state": s, "prompt": prompt}


# ---------------------------------------------------------------- auth


def check_bearer(header: str | None, query_token: str | None, expected: str | None) -> None:
    """Raise PolicyError(401) unless the bearer token matches. Fails closed if no token is configured."""
    if not expected:
        raise PolicyError("auth_not_configured", f"{TOKEN_ENV} is not set on the server; refusing all calls", 503)
    got = ""
    if header and header.lower().startswith("bearer "):
        got = header[7:].strip()
    elif query_token:
        got = query_token
    if not got or not hmac.compare_digest(got.encode(), expected.encode()):
        raise PolicyError("unauthorized", "missing or wrong bearer token", 401)


# ---------------------------------------------------------------- loaded policies + LRU cache


@dataclasses.dataclass
class Loaded:
    key: str
    uri: str  # as the caller sent it (last time)
    spec: PolicySpec
    policy: Any
    load_s: float
    compile_s: float
    loaded_at: float
    gpu_ms: float = 0.0

    def summary(self) -> dict:
        return {
            "checkpoint_uri": self.key,
            "requested_uri": self.uri,
            "spec": dataclasses.asdict(self.spec) | {"action_space": self.spec.action_space},
            "load_s": self.load_s,
            "compile_s": self.compile_s,
            "loaded_at": self.loaded_at,
        }


class PolicyServer:
    """Container-side state. ``loader(resolved) -> (policy, spec)``; ``on_loaded`` e.g. commits the JAX cache."""

    def __init__(
        self,
        mounts: dict[str, str],
        loader: Callable[[ResolvedCheckpoint], tuple[Any, PolicySpec]],
        *,
        capacity: int = 2,
        persist: Callable[[dict], None] | None = None,
        before_load: Callable[[ResolvedCheckpoint], None] | None = None,
        on_loaded: Callable[[], None] | None = None,
        info: dict | None = None,
    ):
        self.mounts = mounts
        self.loader = loader
        self.capacity = max(1, capacity)
        self.persist = persist
        self.before_load = before_load
        self.on_loaded = on_loaded
        self.info = info or {}
        self.cache: collections.OrderedDict[str, Loaded] = collections.OrderedDict()
        self.active: Loaded | None = None
        self.previous_key: str | None = None
        self.loading: str | None = None
        self._load_lock = threading.Lock()  # one load at a time
        self._infer_lock = threading.Lock()  # one forward pass at a time (one GPU)
        self.started_at = time.time()

    # -- loading / activation
    def _load(self, r: ResolvedCheckpoint, uri: str) -> Loaded:
        # Make room first so at most `capacity` policies are ever on the GPU, never evicting the active one.
        while len(self.cache) >= self.capacity:
            victim = next((k for k in self.cache if not self.active or k != self.active.key), None)
            if victim is None:
                break
            self.cache.pop(victim)
            import gc

            gc.collect()
        if self.before_load:
            self.before_load(r)
        self.loading = r.key
        try:
            t0 = time.monotonic()
            policy, spec = self.loader(r)
            load_s = time.monotonic() - t0
            entry = Loaded(r.key, uri, spec, policy, round(load_s, 1), 0.0, time.time())
            _instrument(entry)
            t1 = time.monotonic()
            dummy = build_obs({"state": [0.0] * spec.state_dim}, spec, decode=_decode_image)
            entry.policy.infer(dummy)  # JIT compile now, not on the robot's first call
            entry.policy.infer(dummy)
            entry.compile_s = round(time.monotonic() - t1, 1)
        finally:
            self.loading = None
        self.cache[r.key] = entry
        if self.on_loaded:
            try:
                self.on_loaded()
            except Exception as e:  # noqa: BLE001 - a cache commit failure must not fail the swap
                print(f"[rdf-policy] on_loaded hook failed: {e}", flush=True)
        return entry

    def activate(self, uri: str, preload_only: bool = False) -> dict:
        r = resolve_uri(uri, self.mounts)
        t0 = time.monotonic()
        with self._load_lock:
            hit = r.key in self.cache
            entry = self.cache[r.key] if hit else self._load(r, uri)
            entry.uri = uri or r.key
            self.cache.move_to_end(r.key)
            swapped = False
            if not preload_only and (self.active is None or self.active.key != r.key):
                self.previous_key = self.active.key if self.active else None
                self.active = entry  # atomic pointer swap; in-flight calls finish on the old policy
                swapped = True
            if self.active is not None:
                self.cache.move_to_end(self.active.key)
        total_ms = round((time.monotonic() - t0) * 1000, 1)
        if swapped and self.persist:
            try:
                self.persist({"active": self.active.uri, "previous": self.previous_key, "at": time.time()})
            except Exception as e:  # noqa: BLE001
                print(f"[rdf-policy] could not persist active checkpoint: {e}", flush=True)
        out = {
            "ok": True,
            "active_checkpoint_uri": self.active.key if self.active else None,
            "checkpoint_uri": r.key,
            "preload_only": preload_only,
            "cache_hit": hit,
            "swapped": swapped,
            "swap_ms": total_ms,
            "load_s": None if hit else entry.load_s,
            "compile_s": None if hit else entry.compile_s,
            "cached": list(self.cache),
        }
        print(f"[rdf-policy] activate {json.dumps(out)}", flush=True)
        return out

    # -- inference
    def infer(self, req: dict) -> dict:
        import numpy as np

        entry = self.active
        if entry is None:
            raise PolicyError("not_ready", "no checkpoint is active yet", 503)
        want = req.get("checkpoint_uri")
        if want:
            if resolve_uri(want, self.mounts).key != entry.key:
                raise PolicyError("checkpoint_mismatch", f"active checkpoint is {entry.key}, request expects {want}", 409)
        t0 = time.perf_counter()
        obs = build_obs(req, entry.spec)
        t1 = time.perf_counter()
        with self._infer_lock:
            out = entry.policy.infer(obs)
            gpu_ms = entry.gpu_ms
        t2 = time.perf_counter()
        actions = np.asarray(out["actions"], np.float32)
        return {
            "actions": actions.round(5).tolist(),
            "action_horizon": int(actions.shape[0]),
            "action_dim": int(actions.shape[1]),
            "action_space": entry.spec.action_space,
            "state_dim": entry.spec.state_dim,
            "checkpoint_uri": entry.key,
            "timing": {
                "decode_ms": round((t1 - t0) * 1000, 2),
                "policy_ms": round((t2 - t1) * 1000, 2),
                "gpu_ms": round(gpu_ms, 2),
                "server_ms": round((time.perf_counter() - t0) * 1000, 2),
            },
        }

    def health(self) -> dict:
        return {
            "ok": True,
            "ready": self.active is not None,
            "active_checkpoint_uri": self.active.key if self.active else None,
            "active": self.active.summary() if self.active else None,
            "previous_checkpoint_uri": self.previous_key,
            "cached": [e.summary() for e in self.cache.values()],
            "cache_capacity": self.capacity,
            "loading": self.loading,
            "container_started_at": self.started_at,
            "auth": "bearer",
            **self.info,
        }


def _instrument(entry: Loaded) -> None:
    """Wrap the jitted sampler to time GPU work alone (block until the device is done), as in S-05b."""
    import jax

    orig = entry.policy._sample_actions

    def timed(*args, **kwargs):
        s = time.perf_counter()
        out = jax.block_until_ready(orig(*args, **kwargs))
        entry.gpu_ms = (time.perf_counter() - s) * 1000
        return out

    entry.policy._sample_actions = timed


def openpi_loader(base_dir_resolver: Callable[[], str]) -> Callable[[ResolvedCheckpoint], tuple[Any, PolicySpec]]:
    """The real loader: openpi ``create_trained_policy`` with the per-checkpoint config."""

    def load(r: ResolvedCheckpoint):
        from openpi.policies import policy_config

        if r.kind == "base":
            spec, path = BASE_SPEC, base_dir_resolver()
        else:
            spec, path = infer_spec(r.path), r.path
        return policy_config.create_trained_policy(make_openpi_config(spec), path), spec

    return load


# ---------------------------------------------------------------- FastAPI app


def build_app(server: PolicyServer, token_getter: Callable[[], str | None] = lambda: os.environ.get(TOKEN_ENV)):
    import asyncio

    from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect

    api = FastAPI(title="rdf-policy (TDM-41)")

    def _json(obj: Any, status: int = 200) -> Response:
        return Response(json.dumps(obj), status_code=status, media_type="application/json")

    def _err(e: PolicyError) -> Response:
        return _json({"error": str(e), "code": e.code}, e.status)

    def _auth(request: Request) -> None:
        check_bearer(request.headers.get("authorization"), None, token_getter())

    @api.get("/health")
    def health():
        return _json(server.health())

    @api.get("/policy")
    def policy(request: Request):
        try:
            _auth(request)
        except PolicyError as e:
            return _err(e)
        return _json(server.health())

    # Raw body + json.loads: pydantic body parsing cost ~90 ms per call in S-05b.
    @api.post("/infer")
    async def infer(request: Request):
        try:
            _auth(request)
            req = json.loads(await request.body())
            return _json(await asyncio.to_thread(server.infer, req))
        except PolicyError as e:
            return _err(e)
        except (ValueError, KeyError, TypeError) as e:
            return _json({"error": f"bad request: {e}", "code": "bad_request"}, 400)

    async def _activate(request: Request):
        try:
            _auth(request)
            body = json.loads((await request.body()) or b"{}")
            return _json(
                await asyncio.to_thread(server.activate, body.get("checkpoint_uri", ""), bool(body.get("preload_only")))
            )
        except PolicyError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001 - load failures (corrupt checkpoint, OOM) come back as 500 JSON
            return _json({"error": f"{type(e).__name__}: {str(e)[:500]}", "code": "load_failed"}, 500)

    api.post("/policy/activate")(_activate)
    api.post("/activate")(_activate)

    @api.websocket("/ws")
    async def ws(sock: WebSocket):
        try:
            check_bearer(sock.headers.get("authorization"), sock.query_params.get("token"), token_getter())
        except PolicyError:
            # Reject the handshake (HTTP 403 to the client). No custom code/reason: Modal's ASGI
            # bridge fails to serialize a websocket.close carrying them.
            await sock.close(code=1008)
            return
        await sock.accept()
        try:
            while True:
                msg = await sock.receive_text()
                try:
                    out = await asyncio.to_thread(server.infer, json.loads(msg))
                except PolicyError as e:
                    out = {"error": str(e), "code": e.code}
                except (ValueError, KeyError, TypeError) as e:
                    out = {"error": f"bad request: {e}", "code": "bad_request"}
                await sock.send_text(json.dumps(out))
        except WebSocketDisconnect:
            pass

    return api
