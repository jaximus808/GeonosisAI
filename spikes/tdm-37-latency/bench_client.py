#!/usr/bin/env python3
"""TDM-37 / spike S-05b: round-trip latency benchmark for the Modal policy endpoint.

Runs on the laptop (proxy) today and on the Jetson later. Needs only python3 + numpy + Pillow
(or OpenCV). The websocket mode also needs `pip install websockets`; the modal mode needs `modal`
plus Modal tokens.

  # on the Jetson (same command as on the laptop):
  export POLICY_ENDPOINT_URL=https://...modal.run        # from .env
  python3 bench_client.py --n 100 --mode http --label jetson
  python3 bench_client.py --n 100 --mode ws   --label jetson
  python3 bench_client.py --n 100 --mode http --camera 0,2 --label jetson-realcams   # real frames via OpenCV

Each call sends 2 x 224x224 JPEG (base + wrist cam), an 8-d state and a prompt, and gets back a
50 x 8 action chunk. It reports p50/p95 round trip, the server's own timing (JPEG decode, policy,
GPU-only) and the rest (network + Modal proxy). Results go to results/<label>-<mode>.json.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import io
import json
import os
import platform
import socket
import statistics
import time
import urllib.parse
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent


def _encode_jpeg(arr: np.ndarray, quality: int) -> bytes:
    try:
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except ImportError:
        import cv2

        ok, enc = cv2.imencode(".jpg", arr[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, quality])
        assert ok
        return enc.tobytes()


def synthetic_frame(rng: np.random.Generator) -> np.ndarray:
    """224x224 RGB with camera-like structure (smooth blobs + sensor noise), so JPEG size is realistic."""
    low = rng.integers(0, 256, size=(14, 14, 3)).astype(np.float32)
    img = np.kron(low, np.ones((16, 16, 1), np.float32))
    for _ in range(2):  # cheap blur so edges are not 16px blocks
        img = (img + np.roll(img, 1, 0) + np.roll(img, -1, 0) + np.roll(img, 1, 1) + np.roll(img, -1, 1)) / 5
    img += rng.normal(0, 6, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


class Cameras:
    def __init__(self, spec: str | None):
        self.caps = []
        if spec:
            import cv2

            for idx in spec.split(","):
                cap = cv2.VideoCapture(int(idx))
                if not cap.isOpened():
                    raise SystemExit(f"camera {idx} did not open")
                self.caps.append(cap)
        self.rng = np.random.default_rng(0)

    def frames(self) -> list[np.ndarray]:
        if not self.caps:
            return [synthetic_frame(self.rng), synthetic_frame(self.rng)]
        import cv2

        out = []
        for cap in self.caps:
            ok, bgr = cap.read()
            if not ok:
                raise SystemExit("camera read failed")
            out.append(cv2.resize(bgr, (224, 224))[:, :, ::-1].copy())
        return out


def make_request(cams: Cameras, quality: int, rng: np.random.Generator) -> tuple[dict, float, int]:
    t0 = time.perf_counter()
    jpegs = [_encode_jpeg(f, quality) for f in cams.frames()]
    req = {
        "images": [base64.b64encode(j).decode() for j in jpegs],
        "state": rng.uniform(-1, 1, 8).round(4).tolist(),
        "prompt": "pick up the cup and put it in the bin",
    }
    return req, (time.perf_counter() - t0) * 1000, sum(len(j) for j in jpegs)


class HttpClient:
    """Persistent HTTPS connection (keep-alive), stdlib only."""

    def __init__(self, url: str):
        u = urllib.parse.urlparse(url)
        self.host, self.base = u.netloc, u.path.rstrip("/")
        self.conn = None

    def _connect(self):
        self.conn = http.client.HTTPSConnection(self.host, timeout=120)
        self.conn.connect()
        self.conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def call(self, req: dict) -> dict:
        body = json.dumps(req).encode()
        for attempt in range(2):
            if self.conn is None:
                self._connect()
            try:
                self.conn.request("POST", self.base + "/infer", body, {"Content-Type": "application/json"})
                resp = self.conn.getresponse()
                data = resp.read()
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}: {data[:300]!r}")
                return json.loads(data)
            except (http.client.HTTPException, ConnectionError, OSError):
                self.conn = None
                if attempt:
                    raise
        raise AssertionError

    def get(self, path: str) -> dict:
        if self.conn is None:
            self._connect()
        self.conn.request("GET", self.base + path)
        resp = self.conn.getresponse()
        return json.loads(resp.read())


class WsClient:
    def __init__(self, url: str):
        from websockets.sync.client import connect

        self.ws = connect(url.replace("https://", "wss://").rstrip("/") + "/ws", max_size=2**24, open_timeout=600)

    def call(self, req: dict) -> dict:
        self.ws.send(json.dumps(req))
        return json.loads(self.ws.recv(timeout=120))


class ModalClient:
    def __init__(self, _url: str):
        import modal

        self.obj = modal.Cls.from_name("rdf-s05b-policy", "Policy")()

    def call(self, req: dict) -> dict:
        return self.obj.infer.remote(req)


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(xs: list[float]) -> dict:
    return {
        "p50": round(pct(xs, 50), 1),
        "p95": round(pct(xs, 95), 1),
        "p99": round(pct(xs, 99), 1),
        "mean": round(statistics.fmean(xs), 1),
        "min": round(min(xs), 1),
        "max": round(max(xs), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("POLICY_ENDPOINT_URL"))
    ap.add_argument("--mode", choices=["http", "ws", "modal"], default="http")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--camera", help="OpenCV camera indices, e.g. 0,2 (default: synthetic frames)")
    ap.add_argument("--interval-ms", type=float, default=0, help="sleep between calls (0 = back to back)")
    ap.add_argument("--label", default=platform.node().split(".")[0])
    args = ap.parse_args()
    if not args.url and args.mode != "modal":
        raise SystemExit("set POLICY_ENDPOINT_URL or pass --url")

    cams = Cameras(args.camera)
    rng = np.random.default_rng(1)
    t0 = time.perf_counter()
    client = {"http": HttpClient, "ws": WsClient, "modal": ModalClient}[args.mode](args.url)
    if args.mode == "http":
        health = client.get("/health")  # also wakes a cold container
        print("health:", health, f"({(time.perf_counter() - t0):.1f}s incl. any cold start)")

    rows = []
    for i in range(args.warmup + args.n):
        req, enc_ms, jpeg_bytes = make_request(cams, args.quality, rng)
        s = time.perf_counter()
        out = client.call(req)
        rtt = (time.perf_counter() - s) * 1000
        assert len(out["actions"]) == out["action_horizon"], out.keys()
        if i >= args.warmup:
            t = out["timing"]
            rows.append({"rtt_ms": rtt, "encode_ms": enc_ms, "jpeg_bytes": jpeg_bytes, **t})
        if args.interval_ms:
            time.sleep(args.interval_ms / 1000)

    result = {
        "label": args.label,
        "mode": args.mode,
        "n": len(rows),
        "url": args.url,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": platform.platform(),
        "frames": f"2 x 224x224 JPEG q{args.quality} ({'cameras ' + args.camera if args.camera else 'synthetic'})",
        "jpeg_bytes_mean": round(statistics.fmean(r["jpeg_bytes"] for r in rows)),
        "request_bytes_approx": len(json.dumps(req)),
        "action_chunk": [out["action_horizon"], out["action_dim"]],
        "rtt_ms": summarize([r["rtt_ms"] for r in rows]),
        "server_ms": summarize([r["server_ms"] for r in rows]),
        "gpu_ms": summarize([r["gpu_ms"] for r in rows]),
        "policy_ms": summarize([r["policy_ms"] for r in rows]),
        "decode_ms": summarize([r["decode_ms"] for r in rows]),
        "network_plus_proxy_ms": summarize([r["rtt_ms"] - r["server_ms"] for r in rows]),
        "client_encode_ms": summarize([r["encode_ms"] for r in rows]),
        "raw": [{k: round(v, 2) for k, v in r.items()} for r in rows],
    }
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{args.label}-{args.mode}.json"
    path.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "raw"}, indent=1))
    print("wrote", path)


if __name__ == "__main__":
    main()
