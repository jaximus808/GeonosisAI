"""TDM-50: asynchronous action-chunk client for the remote pi0.5 policy server.

One background thread owns one persistent websocket to the policy server. The control loop
(``rollout.py``) never blocks on the network: it ``submit()``s an observation and keeps executing
the current chunk; the reply (a 50-step chunk, or an error) shows up in ``poll()`` later. At most
one request is in flight. A request that gets no answer within ``request_timeout_s`` counts as
DROPPED: the socket is closed (replies carry no request id, so a late answer on the same socket
could be mistaken for the next one) and the next request reconnects.

Backends, chosen by ONE switch (``POLICY_BACKEND``, or ``PolicyConfig(backend=...)``):

  spike       (default) TDM-37 endpoint, Modal app ``rdf-s05b-policy``: ``POLICY_ENDPOINT_URL``,
              no auth, base pi0.5 only. ``activate()`` accepts only the base checkpoint.
  production  TDM-41 server, Modal app ``rdf-training``: ``POLICY_SERVER_URL`` +
              ``POLICY_SERVER_TOKEN`` (bearer, header on the websocket). ``activate(uri)`` posts
              ``/policy/activate`` (hot-swap), and every request carries ``checkpoint_uri`` so the
              server refuses to answer with another policy's actions (code ``checkpoint_mismatch``).
  sim         [SIM] the local scripted IK policy for the MuJoCo demo (``sim_policy_server.py``, NOT
              pi0.5): ``SIM_POLICY_URL`` (default http://127.0.0.1:8765) + optional ``SIM_POLICY_TOKEN``.
              Same protocol as production (activate + checkpoint_uri on every request).

Wire format (S-05b, both backends; see the "S-05b decision" note)::

    request : {"images": [b64 JPEG base cam 224x224, b64 JPEG wrist cam 224x224],
               "state": [joints..., gripper] (gripper LAST), "prompt": str, "checkpoint_uri"?: str}
    response: {"actions": [[float]*action_dim]*50, "action_horizon", "action_dim",
               "action_space"?, "timing": {decode_ms, policy_ms, gpu_ms, server_ms}}
    error   : {"error": str, "code": str}   (TDM-41; the socket stays open)

Transport fallback: ``transport="http"`` uses POST ``/infer`` on a keep-alive connection instead.
Secrets: the token is read from the environment / repo .env and is never logged.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import threading
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

SPIKE_URL_DEFAULT = "https://jaxonp808--rdf-s05b-policy-policy-web.us-west.modal.run"
PRODUCTION_URL_DEFAULT = "https://jaxonp808--rdf-training-policy-web.us-west.modal.run"
SIM_URL_DEFAULT = "http://127.0.0.1:8765"  # [SIM] sim_policy_server.py
BASE_CHECKPOINT = "gs://openpi-assets/checkpoints/pi05_base"
BASE_ALIASES = {"base", "pi05_base", BASE_CHECKPOINT, BASE_CHECKPOINT + "/", BASE_CHECKPOINT + "/params"}
IMAGE_KEYS = ("observation/image", "observation/wrist_image")  # S-05a keys, base cam first


class PolicyClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class PolicyConfig:
    backend: str = "spike"  # spike | production | sim
    url: str = SPIKE_URL_DEFAULT
    token: str | None = None  # production only; never printed
    transport: str = "ws"  # ws | http
    request_timeout_s: float = 1.0  # no reply by then -> dropped (S-05b: hold at step 40 ~ 1 s)
    connect_timeout_s: float = 10.0
    jpeg_quality: int = 90
    reconnect_backoff_s: tuple[float, ...] = (0.0, 0.25, 0.5, 1.0, 2.0)

    @classmethod
    def from_env(cls, backend: str | None = None, **overrides: Any) -> "PolicyConfig":
        """``POLICY_BACKEND`` picks spike (default) or production; URLs/token from env or .env."""
        try:
            from rdf_common.spacetime_client import load_dotenv

            load_dotenv()
        except Exception:  # noqa: BLE001 - laptop/Jetson without rdf_common .env is fine
            pass
        b = (backend or os.environ.get("POLICY_BACKEND") or "spike").strip().lower()
        if b in ("tdm-41", "tdm41", "prod"):
            b = "production"
        if b not in ("spike", "production", "sim"):
            raise PolicyClientError("bad_config", f"POLICY_BACKEND must be spike|production|sim, got {b!r}")
        if b == "sim":  # [SIM] scripted local policy for the MuJoCo demo (sim_policy_server.py)
            url = os.environ.get("SIM_POLICY_URL") or SIM_URL_DEFAULT
            token = os.environ.get("SIM_POLICY_TOKEN") or None
        elif b == "production":
            url = os.environ.get("POLICY_SERVER_URL") or PRODUCTION_URL_DEFAULT
            token = os.environ.get("POLICY_SERVER_TOKEN") or None
            if not token:
                raise PolicyClientError("bad_config", "POLICY_BACKEND=production needs POLICY_SERVER_TOKEN")
        else:
            url, token = os.environ.get("POLICY_ENDPOINT_URL") or SPIKE_URL_DEFAULT, None
        kw: dict[str, Any] = {"backend": b, "url": url.rstrip("/"), "token": token}
        if os.environ.get("POLICY_TRANSPORT"):
            kw["transport"] = os.environ["POLICY_TRANSPORT"].strip().lower()
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kw)

    @property
    def hot_swap(self) -> bool:
        """The server serves any checkpoint: ``activate(uri)`` + ``checkpoint_uri`` on every request."""
        return self.backend in ("production", "sim")

    def describe(self) -> dict[str, Any]:
        d = {"backend": self.backend, "url": self.url, "transport": self.transport, "auth": bool(self.token),
             "request_timeout_s": self.request_timeout_s}
        if self.backend == "sim":
            d.update(sim=True, policy="[SIM] scripted IK pick-and-place policy (MuJoCo demo stand-in, NOT pi0.5)")
        return d


# --------------------------------------------------------------------------- values


@dataclass
class Observation:
    """What the control loop hands over. Encoding happens on the client thread, not the loop."""

    images: Sequence[np.ndarray]  # [base 224x224 RGB, wrist 224x224 RGB]
    state: Sequence[float]  # joints + gripper (gripper last)
    prompt: str
    t_obs: float  # time.monotonic() when state/images were sampled
    step: int = 0  # control step index at sampling time
    checkpoint_uri: str | None = None
    timeout_s: float | None = None  # override request_timeout_s (warm-up call after a cold start)


@dataclass
class PolicyReply:
    req_id: int
    obs_step: int
    t_obs: float
    t_sent: float
    t_recv: float
    actions: np.ndarray | None = None  # [H, D]
    action_space: str | None = None
    timing: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    code: str | None = None  # network | timeout | server codes (checkpoint_mismatch, not_ready, ...)
    # TDM-114: the server's own phase label for this chunk ([SIM] scripted policy: approach / descend / close /
    # lift / carry / lower / release / retreat / regrasp). Real policies send none: stays None, never invented.
    phase: str | None = None

    @property
    def ok(self) -> bool:
        return self.actions is not None

    @property
    def rtt_ms(self) -> float:
        return (self.t_recv - self.t_sent) * 1000

    @property
    def age_ms(self) -> float:
        """Observation age when the reply landed (encode + queue + RTT)."""
        return (self.t_recv - self.t_obs) * 1000


def encode_jpeg_rgb(img: np.ndarray, quality: int = 90) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".jpg", np.ascontiguousarray(img[:, :, ::-1]), [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise PolicyClientError("encode", "JPEG encode failed")
    return buf.tobytes()


def build_request(obs: Observation, quality: int = 90) -> dict[str, Any]:
    req: dict[str, Any] = {
        "images": [base64.b64encode(encode_jpeg_rgb(im, quality)).decode() for im in obs.images],
        "state": [round(float(v), 5) for v in obs.state],
        "prompt": obs.prompt,
    }
    if obs.checkpoint_uri:
        req["checkpoint_uri"] = obs.checkpoint_uri
    return req


def parse_reply(raw: dict[str, Any]) -> tuple[np.ndarray, str | None, dict[str, Any]]:
    if "error" in raw:
        raise PolicyClientError(str(raw.get("code") or "server_error"), str(raw["error"])[:300])
    acts = np.asarray(raw.get("actions"), dtype=np.float64)
    if acts.ndim != 2 or acts.shape[0] == 0 or not np.all(np.isfinite(acts)):
        raise PolicyClientError("bad_reply", f"actions shape {acts.shape} / non-finite")
    return acts, raw.get("action_space"), dict(raw.get("timing") or {})


# --------------------------------------------------------------------------- transports


class _WsTransport:
    def __init__(self, cfg: PolicyConfig) -> None:
        self.cfg = cfg
        self.ws: Any = None

    def _connect(self) -> None:
        from websockets.sync.client import connect

        url = self.cfg.url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        headers = {"Authorization": f"Bearer {self.cfg.token}"} if self.cfg.token else None
        ws = connect(url, additional_headers=headers, max_size=2**24, open_timeout=self.cfg.connect_timeout_s,
                     compression=None)
        self.ws = ws.__enter__()  # long-lived socket; closed in close() (websockets >= 17 wants the CM protocol)

    def call(self, req: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self.ws is None:
            self._connect()
        self.ws.send(json.dumps(req))
        return json.loads(self.ws.recv(timeout=timeout))

    def close(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


class _HttpTransport:
    def __init__(self, cfg: PolicyConfig) -> None:
        self.cfg = cfg
        u = urllib.parse.urlparse(cfg.url)
        self.https, self.host, self.base = u.scheme == "https", u.netloc, u.path.rstrip("/")
        self.conn: http.client.HTTPConnection | None = None

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.token:
            h["Authorization"] = f"Bearer {self.cfg.token}"
        return h

    def request(self, method: str, path: str, body: dict[str, Any] | None, timeout: float) -> tuple[int, dict[str, Any]]:
        if self.conn is None:
            cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
            self.conn = cls(self.host, timeout=timeout)
            self.conn.connect()
            self.conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.conn.sock.settimeout(timeout)
        data = json.dumps(body).encode() if body is not None else None
        self.conn.request(method, self.base + path, data, self._headers())
        resp = self.conn.getresponse()
        raw = resp.read()
        try:
            out = json.loads(raw) if raw else {}
        except ValueError:
            out = {"error": raw[:300].decode(errors="replace"), "code": f"http_{resp.status}"}
        if resp.status >= 400 and "error" not in out:
            out = {"error": str(out)[:300], "code": f"http_{resp.status}"}
        return resp.status, out

    def call(self, req: dict[str, Any], timeout: float) -> dict[str, Any]:
        return self.request("POST", "/infer", req, timeout)[1]

    def close(self) -> None:
        c, self.conn = self.conn, None
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- the async client


class PolicyClient:
    """Async chunk client. ``start()`` -> ``submit(obs)`` / ``poll()`` from the control loop -> ``close()``."""

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig.from_env()
        self._transport = _HttpTransport(self.config) if self.config.transport == "http" else _WsTransport(self.config)
        self._cv = threading.Condition()
        self._pending: tuple[int, Observation] | None = None
        self._replies: list[PolicyReply] = []
        self._in_flight = False
        self._next_id = 1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fails = 0  # consecutive transport failures (for reconnect backoff)
        self.log: list[PolicyReply] = []  # every reply, for latency stats

    # ---- lifecycle
    def start(self) -> "PolicyClient":
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="rdf-policy", daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._transport.close()

    def __enter__(self) -> "PolicyClient":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- control-loop API (never blocks on the network)
    @property
    def busy(self) -> bool:
        with self._cv:
            return self._in_flight or self._pending is not None

    def submit(self, obs: Observation) -> int | None:
        """Queue one observation. Returns its request id, or None if a request is already in flight."""
        with self._cv:
            if self._in_flight or self._pending is not None:
                return None
            rid = self._next_id
            self._next_id += 1
            self._pending = (rid, obs)
            self._cv.notify_all()
            return rid

    def poll(self) -> list[PolicyReply]:
        """Replies (chunks or errors) that arrived since the last poll, oldest first."""
        with self._cv:
            out, self._replies = self._replies, []
            return out

    def wait_reply(self, timeout: float) -> PolicyReply | None:
        """Blocking helper (warm-up / the sync fallback outside a control loop)."""
        end = time.monotonic() + timeout
        with self._cv:
            while not self._replies and not self._stop.is_set():
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(left)
            return self._replies.pop(0) if self._replies else None

    def infer_blocking(self, obs: Observation, timeout: float) -> PolicyReply:
        self.poll()
        if self.submit(obs) is None:
            raise PolicyClientError("busy", "a request is already in flight")
        r = self.wait_reply(timeout)
        if r is None:
            raise PolicyClientError("timeout", f"no reply within {timeout:.1f}s")
        return r

    # ---- worker thread
    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while self._pending is None and not self._stop.is_set():
                    self._cv.wait(0.2)
                if self._stop.is_set():
                    return
                rid, obs = self._pending  # type: ignore[misc]
                self._pending = None
                self._in_flight = True
            reply = self._do_call(rid, obs)
            with self._cv:
                self._in_flight = False
                self._replies.append(reply)
                self.log.append(reply)
                self._cv.notify_all()

    def _do_call(self, rid: int, obs: Observation) -> PolicyReply:
        if self._fails:  # reconnect backoff after a transport failure, bounded by the timeout budget
            b = self.config.reconnect_backoff_s
            time.sleep(min(b[min(self._fails, len(b) - 1)], 2.0))
        try:
            req = build_request(obs, self.config.jpeg_quality)
        except Exception as e:  # noqa: BLE001
            now = time.monotonic()
            return PolicyReply(rid, obs.step, obs.t_obs, now, now, error=str(e), code="encode")
        t_sent = time.monotonic()
        try:
            raw = self._transport.call(req, obs.timeout_s or self.config.request_timeout_s)
            t_recv = time.monotonic()
            self._fails = 0
        except TimeoutError as e:
            self._transport.close()  # a late reply must not be read as the next request's answer
            self._fails += 1
            return PolicyReply(rid, obs.step, obs.t_obs, t_sent, time.monotonic(), error=str(e) or "timeout", code="timeout")
        except Exception as e:  # noqa: BLE001 - connection refused/closed, DNS, TLS, bad JSON
            self._transport.close()
            self._fails += 1
            return PolicyReply(rid, obs.step, obs.t_obs, t_sent, time.monotonic(),
                               error=f"{type(e).__name__}: {e}"[:300], code="network")
        try:
            acts, space, timing = parse_reply(raw)
        except PolicyClientError as e:
            return PolicyReply(rid, obs.step, obs.t_obs, t_sent, t_recv, error=str(e), code=e.code)
        ph = raw.get("phase") if isinstance(raw, dict) else None
        return PolicyReply(rid, obs.step, obs.t_obs, t_sent, t_recv, actions=acts, action_space=space, timing=timing,
                           phase=ph if isinstance(ph, str) and ph else None)

    # ---- control-plane calls (blocking; not from the control loop)
    def _http(self) -> _HttpTransport:
        return _HttpTransport(self.config)

    def health(self, timeout: float = 10.0) -> dict[str, Any]:
        h = self._http()
        try:
            status, out = h.request("GET", "/health", None, timeout)
        finally:
            h.close()
        if status != 200:
            raise PolicyClientError(str(out.get("code") or f"http_{status}"), str(out.get("error") or out)[:300])
        return out

    def wait_healthy(self, timeout: float = 120.0, poll_s: float = 2.0) -> dict[str, Any]:
        """Poll /health until the server answers (and, on production, ``ready``). Cold start ~33 s."""
        end = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < end:
            try:
                out = self.health(timeout=max(5.0, min(60.0, end - time.monotonic())))
                if not self.config.hot_swap or out.get("ready", True):
                    return out
                last = PolicyClientError("not_ready", f"loading={out.get('loading')}")
            except Exception as e:  # noqa: BLE001
                last = e
            time.sleep(poll_s)
        raise PolicyClientError("unhealthy", f"policy server not healthy after {timeout:.0f}s: {last}")

    def activate(self, checkpoint_uri: str | None, timeout: float = 600.0) -> dict[str, Any]:
        """Make ``checkpoint_uri`` the served policy. Spike: only the base checkpoint exists."""
        uri = (checkpoint_uri or "base").strip()
        if not self.config.hot_swap:
            if uri not in BASE_ALIASES:
                raise PolicyClientError("unsupported", f"the spike endpoint serves only base pi0.5, not {uri!r}; "
                                                       "set POLICY_BACKEND=production (TDM-41) for fine-tuned checkpoints")
            return {"backend": "spike", "checkpoint_uri": BASE_CHECKPOINT, "activated": False, "note": "base only"}
        h = self._http()
        try:
            status, out = h.request("POST", "/policy/activate", {"checkpoint_uri": uri}, timeout)
        finally:
            h.close()
        if status != 200:
            raise PolicyClientError(str(out.get("code") or f"http_{status}"), str(out.get("error") or out)[:300])
        return out


def latency_summary(replies: Sequence[PolicyReply]) -> dict[str, Any]:
    ok = [r.rtt_ms for r in replies if r.ok]
    errs: dict[str, int] = {}
    for r in replies:
        if not r.ok:
            errs[r.code or "error"] = errs.get(r.code or "error", 0) + 1
    out: dict[str, Any] = {"calls": len(replies), "ok": len(ok), "errors": errs}
    if ok:
        a = np.asarray(ok)
        out.update(rtt_p50_ms=round(float(np.percentile(a, 50)), 1), rtt_p95_ms=round(float(np.percentile(a, 95)), 1),
                   rtt_max_ms=round(float(a.max()), 1))
        gpu = [float(r.timing["gpu_ms"]) for r in replies if r.ok and "gpu_ms" in r.timing]
        if gpu:
            out["gpu_p50_ms"] = round(float(np.percentile(gpu, 50)), 1)
    return out


__all__ = ["BASE_CHECKPOINT", "IMAGE_KEYS", "Observation", "PolicyClient", "PolicyClientError", "PolicyConfig",
           "PolicyReply", "build_request", "latency_summary", "parse_reply"]
