#!/usr/bin/env python3
"""TDM-41 proof: base pi0.5 -> hot-swap to the S-05a smoke LoRA -> flip back, with latency numbers.

    make policy-up      # or let the first /health call cold-start it
    cd services/training && uv run --with 'modal>=1.6.0' --with httpx --with websockets --with pillow \
        python scripts/policy_proof.py [--n 30] [--ckpt <uri>] [--out results/tdm-41-proof.json]
    make policy-down

It sends the S-05b payload (two 224x224 q90 JPEGs + state + prompt) over the websocket, the way
the robot client does. Images are synthetic noise: this measures latency and shapes, not task
skill. Runs from the laptop or the Jetson (set POLICY_SERVER_URL and POLICY_SERVER_TOKEN there).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import pathlib
import statistics
import sys
import time

import httpx
import numpy as np
import websockets
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from policy_ctl import token, url  # noqa: E402

SMOKE = "modal-vol://rdf-s05a-checkpoints/checkpoints/rdf_s05a_pi05_lora/smoke_lora_h100/199"


def jpeg(seed: int) -> str:
    rng = np.random.default_rng(seed)
    arr = (rng.random((224, 224, 3)) * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 1)


async def ws_bench(base: str, tok: str, state_dim: int, n: int, expect: str) -> dict:
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    imgs = [jpeg(1), jpeg(2)]
    rtts, gpu, server = [], [], []
    first = None
    async with websockets.connect(ws_url, additional_headers={"Authorization": f"Bearer {tok}"}, max_size=2**24) as ws:
        for i in range(n + 2):  # 2 warm-up calls not counted
            req = {"images": imgs, "state": [0.01 * i] * state_dim, "prompt": "pick up the cup", "checkpoint_uri": expect}
            t0 = time.perf_counter()
            await ws.send(json.dumps(req))
            out = json.loads(await ws.recv())
            dt = (time.perf_counter() - t0) * 1000
            if "error" in out:
                raise RuntimeError(f"infer error: {out}")
            if i >= 2:
                rtts.append(dt)
                gpu.append(out["timing"]["gpu_ms"])
                server.append(out["timing"]["server_ms"])
            first = first or out
    return {
        "n": n,
        "rtt_ms": {"p50": pct(rtts, 50), "p95": pct(rtts, 95), "max": round(max(rtts), 1), "mean": round(statistics.mean(rtts), 1)},
        "gpu_ms_p50": pct(gpu, 50),
        "server_ms_p50": pct(server, 50),
        "chunk_shape": [first["action_horizon"], first["action_dim"]],
        "action_space": first["action_space"],
        "checkpoint_uri": first["checkpoint_uri"],
        "first_action": [round(x, 4) for x in first["actions"][0]],
        "last_action": [round(x, 4) for x in first["actions"][-1]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--ckpt", default=SMOKE)
    ap.add_argument("--out", default=str(pathlib.Path(__file__).resolve().parents[1] / "results" / "tdm-41-proof.json"))
    a = ap.parse_args()
    base, tok = url(), token()
    auth = {"Authorization": f"Bearer {tok}"}
    c = httpx.Client(timeout=900)
    res: dict = {"url": base, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    t0 = time.monotonic()
    while True:
        try:
            h = c.get(f"{base}/health").json()
            if h.get("ready"):
                break
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(3)
    res["health_wait_s"] = round(time.monotonic() - t0, 1)
    res["health_initial"] = {k: h.get(k) for k in ("active_checkpoint_uri", "gpu", "startup_s", "cached")}
    print("health:", json.dumps(res["health_initial"])[:400], flush=True)

    # auth is enforced
    res["unauthorized"] = {
        "infer_no_token": c.post(f"{base}/infer", content=b"{}").status_code,
        "activate_wrong_token": c.post(f"{base}/policy/activate", json={}, headers={"Authorization": "Bearer nope"}).status_code,
    }
    try:
        asyncio.run(_ws_noauth(base))
        res["unauthorized"]["ws_no_token"] = "ACCEPTED (bad)"
    except Exception as e:  # noqa: BLE001
        res["unauthorized"]["ws_no_token"] = f"rejected ({type(e).__name__})"
    print("unauthorized:", res["unauthorized"], flush=True)

    steps = [("activate_base", "base", 8), ("activate_finetuned", a.ckpt, None), ("flip_to_base", "base", 8), ("flip_to_finetuned", a.ckpt, None)]
    for name, uri, sdim in steps:
        t = time.monotonic()
        r = c.post(f"{base}/policy/activate", json={"checkpoint_uri": uri}, headers=auth)
        wall = round((time.monotonic() - t) * 1000, 1)
        body = r.json()
        if r.status_code >= 300:
            print(name, r.status_code, body)
            res[name] = {"status": r.status_code, **body}
            break
        hh = c.get(f"{base}/policy", headers=auth).json()
        spec = hh["active"]["spec"]
        bench = asyncio.run(ws_bench(base, tok, sdim or spec["state_dim"], a.n, uri))
        res[name] = {"activate": {**body, "wall_ms": wall}, "spec": spec, "infer_ws": bench}
        print(name, json.dumps({"activate": {k: body.get(k) for k in ("cache_hit", "swap_ms", "load_s", "compile_s")}, "wall_ms": wall,
                                "rtt": bench["rtt_ms"], "gpu_ms_p50": bench["gpu_ms_p50"], "shape": bench["chunk_shape"],
                                "space": bench["action_space"]}), flush=True)
    res["health_final"] = c.get(f"{base}/health").json()
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2))
    print("wrote", out)
    return 0


async def _ws_noauth(base: str) -> None:
    ws_url = base.replace("https://", "wss://") + "/ws"
    async with websockets.connect(ws_url) as ws:
        await ws.send("{}")
        await ws.recv()


if __name__ == "__main__":
    raise SystemExit(main())
