"""TDM-50: local FAKE policy server for laptop tests. Same wire format as the S-05b / TDM-41 server.

Everything it returns is synthetic ([FAKE]); it exists to exercise the rollout's chunk stitching,
hold and retry logic with controlled latency, jitter, drops and errors. Serves:

  GET  /health   {"ok": true, "ready": true, "fake": true}
  WS   /ws       one JSON request -> one JSON reply (or nothing for a dropped call)

Behaviour per call comes from ``schedule(call_index) -> Behaviour``: latency in seconds and one of
``ok`` | ``drop`` (never answer) | ``error`` (``{"error","code"}``) | ``close`` (drop the socket).

Chunk content (``trajectory``):
  ramp   absolute joint targets ``state + (j+1) * velocity * dt`` (``action_space='joint_pos_abs'``),
         a constant-velocity line through the observed state. Correct latency skipping makes the
         stitched command a smooth ramp; a wrong skip shows up as a jump of ``skip * velocity * dt``.
  droid  8-d pseudo-random "base pi0.5" output (no ``action_space``), like the spike endpoint.
"""
from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Behaviour:
    latency_s: float = 0.15
    kind: str = "ok"  # ok | drop | error | close
    code: str = "fake_error"


def jitter_schedule(latency_ms: float = 160, jitter_ms: float = 30, drop_prob: float = 0.0, error_prob: float = 0.0,
                    seed: int = 0) -> Callable[[int], Behaviour]:
    rng = random.Random(seed)

    def f(i: int) -> Behaviour:
        lat = max(0.0, rng.gauss(latency_ms, jitter_ms)) / 1000
        u = rng.random()
        kind = "drop" if u < drop_prob else ("error" if u < drop_prob + error_prob else "ok")
        return Behaviour(lat, kind)

    return f


def scripted_schedule(items: Sequence[Behaviour], default: Behaviour = Behaviour()) -> Callable[[int], Behaviour]:
    return lambda i: items[i] if i < len(items) else default


@dataclass
class CallRecord:
    index: int
    t_recv: float
    behaviour: Behaviour
    state: list[float] = field(default_factory=list)
    prompt: str = ""
    n_images: int = 0
    image_bytes: int = 0
    headers_auth: bool = False


class FakePolicyServer:
    def __init__(self, *, schedule: Callable[[int], Behaviour] | None = None, trajectory: str = "ramp",
                 velocity: Sequence[float] = (0.2, -0.1, 0.1, 0.0, 0.0, 0.0), dt: float = 1 / 30,
                 horizon: int = 50, host: str = "127.0.0.1", port: int = 0, require_token: str | None = None) -> None:
        self.schedule = schedule or jitter_schedule()
        self.trajectory = trajectory
        self.velocity = np.asarray(velocity, dtype=float)
        self.dt = dt
        self.horizon = horizon
        self.host, self.port = host, port
        self.require_token = require_token
        self.calls: list[CallRecord] = []
        self._lock = threading.Lock()
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._rng = np.random.default_rng(0)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "FakePolicyServer":
        from websockets.sync.server import serve

        self._server = serve(self._handle, self.host, self.port, process_request=self._process_request,
                             max_size=2**24, compression=None)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-policy", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    # ---- http
    def _process_request(self, conn: Any, request: Any) -> Any:
        path = request.path.split("?")[0]
        if path == "/health":
            body = json.dumps({"ok": True, "ready": True, "fake": True, "gpu": "[FAKE]"})
            resp = conn.respond(HTTPStatus.OK, body)
            resp.headers["Content-Type"] = "application/json"
            return resp
        if path != "/ws":
            return conn.respond(HTTPStatus.NOT_FOUND, "not found\n")
        if self.require_token and request.headers.get("Authorization") != f"Bearer {self.require_token}":
            return conn.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    # ---- websocket
    def _handle(self, ws: Any) -> None:
        for msg in ws:
            t_recv = time.monotonic()
            req = json.loads(msg)
            with self._lock:
                i = len(self.calls)
                b = self.schedule(i)
                self.calls.append(CallRecord(i, t_recv, b, list(req.get("state") or []), str(req.get("prompt") or ""),
                                             len(req.get("images") or []), sum(len(s) for s in req.get("images") or []),
                                             bool(ws.request.headers.get("Authorization"))))
            time.sleep(b.latency_s)
            if b.kind == "drop":
                continue
            if b.kind == "close":
                ws.close()
                return
            if b.kind == "error":
                ws.send(json.dumps({"error": "[FAKE] injected server error", "code": b.code}))
                continue
            ws.send(json.dumps(self._chunk(req, b)))

    def _chunk(self, req: dict[str, Any], b: Behaviour) -> dict[str, Any]:
        state = np.asarray(req.get("state") or [0.0] * 7, dtype=float)
        h = self.horizon
        if self.trajectory == "ramp":
            dof = len(self.velocity)
            j = np.arange(1, h + 1)[:, None]
            joints = state[:dof][None] + j * self.velocity[None] * self.dt
            acts = np.concatenate([joints, np.full((h, 1), 0.5)], axis=1)
            space: str | None = "joint_pos_abs"
        else:
            acts = self._rng.normal(0, 0.3, (h, 8))
            acts[:, -1] = self._rng.uniform(0, 1, h)
            space = None
        out: dict[str, Any] = {"actions": acts.round(6).tolist(), "action_horizon": h, "action_dim": acts.shape[1],
                               "timing": {"decode_ms": 1.0, "policy_ms": round(b.latency_s * 1000, 1), "gpu_ms": 0.0,
                                          "server_ms": round(b.latency_s * 1000, 1)}, "fake": True}
        if space:
            out["action_space"] = space
        return out


__all__ = ["Behaviour", "CallRecord", "FakePolicyServer", "jitter_schedule", "scripted_schedule"]
