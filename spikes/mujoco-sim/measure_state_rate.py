"""[SIM] /ws/state rate + control_source while a practice session drives the MuJoCo robot (vs idle).

    cd services/robot-client && uv run --extra sim python ../../spikes/mujoco-sim/measure_state_rate.py \
        --orch http://127.0.0.1:8083 --operator-key-file <run>/tokens/operator.key --robot 1
"""
from __future__ import annotations

import argparse
import collections
import json
import threading
import time
import urllib.request

from websockets.sync.client import connect


def post(url, body, key):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "x-rdf-operator-key": key}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orch", default="http://127.0.0.1:8083")
    ap.add_argument("--operator-key-file", required=True)
    ap.add_argument("--robot", type=int, default=1)
    ap.add_argument("--seconds", type=float, default=6.0)
    a = ap.parse_args()
    key = open(a.operator_key_file).read().strip()
    r = post(f"{a.orch}/robots/{a.robot}/practice/start", {}, key)
    tok = r["token"]
    time.sleep(2.0)
    samples: list[tuple[float, str]] = []
    stop = threading.Event()

    def watch():
        with connect(r["state_url"]) as ws:
            ws.send(json.dumps({"type": "auth", "token": tok}))
            while not stop.is_set():
                m = json.loads(ws.recv(timeout=5))
                if m.get("t") == "state":
                    samples.append((time.monotonic(), m.get("control_source")))

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    time.sleep(a.seconds)
    t_idle = time.monotonic()
    with connect(r["control_url"]) as ws:
        ws.send(json.dumps({"t": "auth", "token": tok}))
        drain = threading.Thread(target=lambda: [ws.recv() for _ in iter(int, 1)], daemon=True)
        drain.start()
        t0 = time.monotonic()
        seq = 0
        while time.monotonic() - t0 < a.seconds:
            seq += 1
            ph = (time.monotonic() - t0) / a.seconds
            import math
            ws.send(json.dumps({"t": "cmd", "seq": seq, "ts_ms": int(time.time() * 1000), "clutch": True,
                                "d_pos": [0.0, 0.05 * math.sin(2 * math.pi * ph), 0.03 * math.sin(4 * math.pi * ph)],
                                "d_rot": [0, 0, 0], "gripper": 1.0, "speed_scale": 1.0}))
            time.sleep(1 / 30)
        t_drive_end = time.monotonic()
        ws.send(json.dumps({"t": "cmd", "seq": seq + 1, "ts_ms": 0, "clutch": False, "d_pos": [0, 0, 0], "d_rot": [0, 0, 0]}))
    stop.set()
    idle = [s for s in samples if s[0] < t_idle]
    drive = [s for s in samples if t0 + 0.5 <= s[0] <= t_drive_end]
    print(json.dumps({"idle_hz": round(len(idle) / a.seconds, 1),
                      "drive_hz": round(len(drive) / (t_drive_end - t0 - 0.5), 1),
                      "drive_sources": dict(collections.Counter(s[1] for s in drive))}))
    post(f"{a.orch}/robots/{a.robot}/practice/stop", {}, key)


if __name__ == "__main__":
    main()
