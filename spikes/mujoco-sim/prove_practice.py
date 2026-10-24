"""[SIM] Proof: practice session on the MuJoCo robot (no lease, no recording) + reset_scene + /ws/state scene.

    cd services/robot-client && uv run --extra sim python ../../spikes/mujoco-sim/prove_practice.py \
        --orch http://127.0.0.1:8083 --operator-key-file <file> --robot 1
Prints JSON lines; never prints the key or tokens.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

from websockets.sync.client import connect


def post(url: str, body: dict, key: str | None) -> tuple[int, dict]:
    h = {"content-type": "application/json", **({"x-rdf-operator-key": key} if key else {})}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orch", default="http://127.0.0.1:8083")
    ap.add_argument("--operator-key-file", required=True)
    ap.add_argument("--robot", type=int, default=1)
    a = ap.parse_args()
    key = open(a.operator_key_file).read().strip()
    st, r = post(f"{a.orch}/robots/{a.robot}/practice/start", {}, None)
    print(json.dumps({"step": "start without auth", "status": st, "detail": r.get("detail")}))
    st, r = post(f"{a.orch}/robots/{a.robot}/practice/start", {}, key)
    print(json.dumps({"step": "start as operator", "status": st, **{k: r.get(k) for k in ("control_url", "state_url", "practice", "command_id")}}))
    tok = r["token"]
    time.sleep(2.0)  # the robot picks up start_teleop
    with connect(r["state_url"]) as sws:
        sws.send(json.dumps({"type": "auth", "token": tok}))
        for _ in range(10):
            m = json.loads(sws.recv(timeout=5))
            if m.get("t") == "state":
                sc = m.get("scene") or {}
                print(json.dumps({"step": "/ws/state scene", "objects": sorted((sc.get("objects") or {}).keys()),
                                  "has_cube": "cube" in sc, "has_plate": "plate" in sc, "robot_state": m.get("robot_state")}))
                break
    with connect(r["control_url"]) as ws:
        ws.send(json.dumps({"t": "auth", "token": tok}))
        hello = None
        t_end = time.time() + 5
        while time.time() < t_end:
            m = json.loads(ws.recv(timeout=5))
            if m.get("t") in ("hello", "auth_ok", "state"):
                hello = m
                if m.get("t") == "state":
                    break
        p0 = hello.get("pose") if hello else None
        ws.send(json.dumps({"t": "record_start", "req": 1}))
        seq = 0
        rec_ack = None
        for i in range(60):  # 2 s of clutch-down, moving +5 cm in y
            seq += 1
            ws.send(json.dumps({"t": "cmd", "seq": seq, "ts_ms": int(time.time() * 1000), "clutch": True,
                                "d_pos": [0.0, 0.05 * min(1.0, i / 40), 0.0], "d_rot": [0, 0, 0], "gripper": 1.0, "speed_scale": 1.0}))
            time.sleep(1 / 30)
        ws.send(json.dumps({"t": "cmd", "seq": seq + 1, "ts_ms": int(time.time() * 1000), "clutch": False,
                            "d_pos": [0, 0, 0], "d_rot": [0, 0, 0], "gripper": 1.0}))
        ws.send(json.dumps({"t": "reset_scene", "req": 2}))
        last = None
        reset = None
        t_end = time.time() + 4
        while time.time() < t_end:
            m = json.loads(ws.recv(timeout=5))
            if m.get("op") == "record_start":
                rec_ack = m
            if m.get("op") == "reset_scene":
                reset = m
            if m.get("t") == "state":
                last = m
        moved = None
        if p0 and last:
            moved = round(last["pose"]["xyz"][1] - p0["xyz"][1], 4)
        print(json.dumps({"step": "drive", "mode": last.get("mode") if last else None, "moved_y_m": moved,
                          "recording": (last or {}).get("recording"), "record_start": {k: (rec_ack or {}).get(k) for k in ("ok", "code")},
                          "reset_scene": {k: (reset or {}).get(k) for k in ("ok", "scene_seq", "objects")}}))
    st, r = post(f"{a.orch}/robots/{a.robot}/practice/stop", {}, key)
    print(json.dumps({"step": "stop", "status": st, **r}))
    time.sleep(2)
    st, r = post(f"{a.orch}/robots/{a.robot}/sim/reset_scene", {}, key)
    print(json.dumps({"step": "reset_scene (idle, robot command)", "status": st, **r}))


if __name__ == "__main__":
    main()
