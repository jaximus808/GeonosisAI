# TDM-37 / S-05b: policy latency, Jetson to a Modal pi0.5 endpoint

The base openpi pi0.5 checkpoint (`gs://openpi-assets/checkpoints/pi05_base`, openpi @ `215abfb`) is served from Modal app `rdf-s05b-policy`.

The endpoint is `POLICY_ENDPOINT_URL=https://jaxonp808--rdf-s05b-policy-policy-web.us-west.modal.run`. It has three routes:

- `GET /health`
- `POST /infer`
- websocket `/ws`, which takes the same JSON as `/infer`, one message per call.

It **scales to zero**, so warm it before a robot session.

| File | What it is |
|---|---|
| `modal_policy.py` | Modal app: image, checkpoint Volume, `Policy` class (FastAPI `/infer` + `/ws`, plus `Policy.infer` Modal method) |
| `bench_client.py` | Latency benchmark. Needs only python3 + numpy + Pillow/OpenCV (+ `websockets` for ws mode). Runs on the laptop or the Jetson. |
| `warm.py` | `up` / `down` / `status`: sets `min_containers` without redeploying |
| `results/*.json` | Raw per-call timings from the laptop runs |

## Payload

```jsonc
// request
{"images": ["<b64 JPEG base cam 224x224>", "<b64 JPEG wrist cam 224x224>"],
 "state":  [j1, j2, j3, j4, j5, j6, j7?, gripper],   // <= 8 floats, gripper LAST
 "prompt": "pick up the cup and put it in the bin"}
// response
{"actions": [[8 floats] x 50], "action_horizon": 50, "action_dim": 8,
 "timing": {"decode_ms": 1.1, "policy_ms": 53, "gpu_ms": 41, "server_ms": 54}}
```

The request is about 45 KB, of which the two q90 JPEGs are about 34 KB. The base model uses the DROID input layout and the base checkpoint's `droid` norm stats. The latency numbers are real, but the actions mean nothing for the YAM until the fine-tuned config (TDM-36/41) defines the YAM state and action mapping.

## Results (laptop on Cal Hacks Wi-Fi as the Jetson proxy, 100 calls each, 2026-10-24)

| GPU (us-west) | transport | RTT p50 | RTT p95 | RTT p99 | GPU-only p50 | server p50 | network + Modal proxy p50 |
|---|---|---|---|---|---|---|---|
| H100 | websocket | **157 ms** | **183 ms** | 346 ms | **41 ms** | 53 ms | 104 ms |
| H100 | HTTP keep-alive | 168 ms | 184 ms | 341 ms | 41 ms | 55 ms | 113 ms |
| H100 | Modal client `.remote()` | 159 ms | 269 ms | 520 ms | 41 ms | 53 ms | 106 ms |
| H100 | websocket, paced at ~3 Hz | 179 ms | 229 ms | 260 ms | 41 ms | 53 ms | 126 ms |
| A100-40GB | websocket | 279 ms | 302 ms | 674 ms | 81 ms | 98 ms | 182 ms |
| A100-40GB | HTTP keep-alive | 306 ms | 331 ms | 366 ms | 81 ms | 97 ms | 208 ms |

- The raw TCP connect to the us-west ingress takes about 50 ms, and the Modal proxy adds about 50–60 ms per call.
- Cold start was 33 s: container start, 12.6 s checkpoint load from the Volume, and 5 s of JIT from the persistent XLA cache. L40S in us-west would not schedule at all, so the GPU setting is a fallback list (H100 first).

## Run it on the Jetson (still pending: JETSON_HOST is not reachable yet)

```bash
uv run --with modal python warm.py up                  # on the laptop; wait for GET /health to answer (~35 s cold)
# on the Jetson:
pip install numpy pillow websockets
export POLICY_ENDPOINT_URL=https://jaxonp808--rdf-s05b-policy-policy-web.us-west.modal.run
python3 bench_client.py --n 100 --mode ws   --label jetson
python3 bench_client.py --n 100 --mode http --label jetson
python3 bench_client.py --n 100 --mode ws --interval-ms 180 --camera 0,2 --label jetson-realcams-3hz
uv run --with modal python warm.py down                # on the laptop, right after
```

Each command writes its results to `results/jetson-*.json`.
