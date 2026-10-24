"""[SIM] Local stand-in policy server for the MuJoCo demo: scripted IK pick-and-place, NOT pi0.5.

Speaks the TDM-41 production wire format, so the TDM-50 rollout (``policy_client.PolicyClient``,
``POLICY_BACKEND=sim``) and the orchestrator's ``POST /policy/activate`` use it unchanged:

  GET  /health            {"ok", "ready", "sim": true, "policy": <label>, "active_checkpoint_uri", profile...}
  POST /policy/activate   {checkpoint_uri, preload_only?} -> picks the noise profile (``sim_policy.profile_for``)
  WS   /ws                one JSON request -> one JSON reply: {"actions": [[7 floats] x 50],
                          "action_space": "joint_pos_abs", "action_horizon", "action_dim", "timing",
                          "sim": true, "policy", "profile", "phase"}; errors {"error", "code"} like TDM-41
                          (``checkpoint_mismatch`` when a request names another checkpoint than the active one)
  POST /infer             the same over HTTP (``POLICY_TRANSPORT=http``)

It reads the scene (cube pose, finger contacts, task status) from the sim robot's loopback
``/sim/state`` (``--state-url``, robot env ``RDF_SIM_STATE_PORT``) and plans with
``sim_policy.ScriptedPickPlace``. Latency like the H100 endpoint is injected with TDM-50's
``fake_policy_server.jitter_schedule`` (default 120 +- 20 ms), so the async chunk executor sees a
realistic round trip. Optional bearer auth: ``SIM_POLICY_TOKEN`` (the orchestrator sends
``POLICY_SERVER_TOKEN``; the demo sets both to one throwaway value).

    cd services/robot-client && uv run --extra sim python -m rdf_robot_client.sim_policy_server \\
        --port 8765 --state-url http://127.0.0.1:8792/sim/state
"""
# No `from __future__ import annotations` here: FastAPI resolves the endpoint annotations at runtime
# (WebSocket / Request are imported inside build_app).
import argparse
import asyncio
import json
import os
import threading
import time
from typing import Any

from .fake_policy_server import jitter_schedule
from .policy_client import BASE_CHECKPOINT
from .sim_policy import POLICY_LABEL, POLICY_NAME, ScriptedPickPlace, profile_for


class SimPolicy:
    """The server's state: the active checkpoint (-> noise profile), the planner, the scene reader."""

    def __init__(self, state_url: str, *, latency_ms: float = 120.0, jitter_ms: float = 20.0, seed: int = 7,
                 token: str | None = None) -> None:
        import httpx

        self.state_url = state_url
        self.http = httpx.Client(timeout=2.0)
        self.planner = ScriptedPickPlace(seed=seed)
        self.schedule = jitter_schedule(latency_ms, jitter_ms, seed=seed)
        self.token = token or None
        self.active_uri = BASE_CHECKPOINT
        self.calls = 0
        self.errors = 0
        self.last_phase: str | None = None
        self._lock = threading.Lock()

    @property
    def profile(self):  # noqa: ANN201
        return profile_for(self.active_uri)

    def health(self) -> dict[str, Any]:
        return {"ok": True, "ready": True, "loading": False, "sim": True, "gpu": "none ([SIM] scripted policy, no model)",
                "policy": POLICY_LABEL, "policy_name": POLICY_NAME, "active_checkpoint_uri": self.active_uri,
                **self.profile.describe(), "calls": self.calls, "errors": self.errors, "last_phase": self.last_phase}

    def activate(self, body: dict[str, Any]) -> dict[str, Any]:
        uri = str(body.get("checkpoint_uri") or "base").strip()
        if uri in ("base", "pi05_base"):
            uri = BASE_CHECKPOINT
        if not body.get("preload_only"):
            with self._lock:
                self.active_uri = uri
        prof = profile_for(uri)
        return {"ok": True, "activated": not body.get("preload_only"), "checkpoint_uri": uri, "sim": True,
                "policy": POLICY_LABEL, **prof.describe(), "load_ms": 0,
                "note": "[SIM] no weights are loaded: the checkpoint URI only picks the scripted policy's noise profile"}

    def answer(self, req: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        with self._lock:
            i = self.calls
            self.calls += 1
            active = self.active_uri
        uri = req.get("checkpoint_uri")
        if uri and str(uri) not in (active, "base") and not (str(uri).rstrip("/").endswith("pi05_base") and active == BASE_CHECKPOINT):
            self.errors += 1
            return {"error": f"[SIM] active checkpoint is {active!r}, request asked for {uri!r}", "code": "checkpoint_mismatch"}
        state = req.get("state") or []
        if len(state) < 7:
            self.errors += 1
            return {"error": "state must be 6 joints + gripper", "code": "bad_request"}
        try:
            r = self.http.get(self.state_url)
            r.raise_for_status()
            scene = r.json()
        except Exception as e:  # noqa: BLE001
            self.errors += 1
            return {"error": f"[SIM] sim state unavailable at {self.state_url}: {e}"[:300], "code": "sim_state_unavailable"}
        acts, info = self.planner.plan([float(v) for v in state], scene, profile_for(active))
        self.last_phase = info["phase"]
        plan_ms = (time.monotonic() - t0) * 1000
        b = self.schedule(i)
        wait = max(0.0, b.latency_s - plan_ms / 1000)
        time.sleep(wait)
        ms = round((time.monotonic() - t0) * 1000, 1)
        return {"actions": acts.round(6).tolist(), "action_horizon": int(acts.shape[0]), "action_dim": int(acts.shape[1]),
                "action_space": "joint_pos_abs", "timing": {"decode_ms": 0.0, "policy_ms": round(plan_ms, 1), "gpu_ms": 0.0,
                                                            "server_ms": ms, "injected_latency_ms": round(wait * 1000, 1)},
                "sim": True, "policy": POLICY_NAME, "checkpoint_uri": active, **info}


def build_app(policy: SimPolicy):  # noqa: ANN201
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse

    app = FastAPI(title="rdf sim policy server ([SIM] scripted, not pi0.5)")

    def _authed(headers: Any) -> bool:
        return not policy.token or headers.get("authorization") == f"Bearer {policy.token}"

    @app.get("/health")
    def health() -> dict[str, Any]:
        return policy.health()

    @app.post("/policy/activate")
    async def activate(request: Request) -> Any:
        if not _authed(request.headers):
            return JSONResponse({"error": "unauthorized", "code": "unauthorized"}, status_code=401)
        body = await request.json()
        return policy.activate(body if isinstance(body, dict) else {})

    @app.post("/infer")
    async def infer(request: Request) -> Any:
        if not _authed(request.headers):
            return JSONResponse({"error": "unauthorized", "code": "unauthorized"}, status_code=401)
        return await asyncio.to_thread(policy.answer, await request.json())

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        if not _authed(sock.headers):
            await sock.close(code=4401)
            return
        await sock.accept()
        try:
            while True:
                raw = await sock.receive_text()
                try:
                    req = json.loads(raw)
                except ValueError:
                    await sock.send_text(json.dumps({"error": "bad json", "code": "bad_request"}))
                    continue
                out = await asyncio.to_thread(policy.answer, req)
                await sock.send_text(json.dumps(out))
        except WebSocketDisconnect:
            return

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rdf_robot_client.sim_policy_server",
                                 description="[SIM] scripted IK pick-and-place policy server (NOT pi0.5)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--state-url", default=os.environ.get("SIM_STATE_URL") or "http://127.0.0.1:8792/sim/state")
    ap.add_argument("--latency-ms", type=float, default=120.0)
    ap.add_argument("--jitter-ms", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    import uvicorn

    pol = SimPolicy(a.state_url, latency_ms=a.latency_ms, jitter_ms=a.jitter_ms, seed=a.seed,
                    token=os.environ.get("SIM_POLICY_TOKEN"))
    print(json.dumps({"svc": "sim-policy", "msg": "[SIM] scripted policy server up", "url": f"http://{a.host}:{a.port}",
                      "state_url": a.state_url, "policy": POLICY_LABEL, "auth": bool(pol.token)}), flush=True)
    uvicorn.run(build_app(pol), host=a.host, port=a.port, log_level="warning", ws_max_size=2**24)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
