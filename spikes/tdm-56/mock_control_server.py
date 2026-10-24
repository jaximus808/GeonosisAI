"""TDM-56 mock robot: a stand-in for the TDM-55 stream server + TDM-52 ``/control`` socket.

[MOCK] Nothing here touches hardware. It exists so the teleop page (apps/web /teleop/[taskId]) can be
tested before TDM-52 lands. It speaks:

* ``/ws/cameras``  the TDM-55 ``jpeg-ws/1`` protocol (auth, hello, ping/pong clock sync, ack pacing),
  with two synthetic cameras that draw the simulated hand: ``top`` (x/y from above) and ``wrist``
  (gripper opening + height).
* ``/ws/control``  the worker-guide teleop contract: ``{t:"cmd"}`` deltas from clutch-down, ``stop``,
  ``takeover``/``handback``/``record_*``; replies ``{t:"ack", seq}`` per cmd and ``{t:"state"}`` at 10 Hz.
  A 200 ms watchdog holds the hand if cmds stop while the clutch is down; ``stop`` latches.
* debug HTTP (no auth, local only):
    GET /debug/freeze?s=3      stop sending camera frames for 3 s (stale-feed test)
    GET /debug/delay?ms=600    stamp frames 600 ms in the past (latency test; 0 to clear)
    GET /debug/robot_stop      latch a stop as if someone hit it at the arm
    GET /debug/clear           clear the latched stop (the "human at the arm")
    GET /debug/stats           JSON: cmd rate, last cmd, stop log, pose
    GET /healthz

Any non-empty token is accepted. Prints one line per second with the received cmd rate.

    services/robot-client/.venv/bin/python spikes/tdm-56/mock_control_server.py --port 8156
    open http://localhost:3737/teleop/0?dev_url=ws://127.0.0.1:8156/ws/cameras&token=mock
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
import time
from collections import deque
from urllib.parse import parse_qs, urlsplit

import cv2
import numpy as np
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

BOX_LO = np.array([0.15, -0.30, 0.02])  # workspace box (m), like TDM-45's
BOX_HI = np.array([0.55, 0.30, 0.45])
HOME = np.array([0.35, 0.0, 0.20])
MAX_LIN = 0.12  # m/s at speed_scale 1
WATCHDOG_S = 0.2


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ])


def q_from_rotvec(v):
    th = float(np.linalg.norm(v))
    if th < 1e-12:
        return np.array([0, 0, 0, 1.0])
    s = math.sin(th / 2) / th
    return np.array([v[0] * s, v[1] * s, v[2] * s, math.cos(th / 2)])


class Robot:
    def __init__(self) -> None:
        self.pos = HOME.copy()
        self.quat = np.array([0, 0, 0, 1.0])
        self.gripper = 1.0
        self.target = self.pos.copy()
        self.target_q = self.quat.copy()
        self.gripper_cmd = 1.0
        self.anchor_pos = self.pos.copy()
        self.anchor_q = self.quat.copy()
        self.clutch = False
        self.last_cmd_at = 0.0
        self.watchdog = False
        self.stopped = False
        self.speed_scale = 0.25
        self.control_source = "idle"
        self.recording = False
        self.clamps: dict[str, float] = {}  # kind -> last hit time
        self.cmd_times: deque[float] = deque()
        self.cmd_count = 0
        self.last_cmd: dict | None = None
        self.stop_log: list[dict] = []
        self.one_way_ms: deque[float] = deque(maxlen=100)

    def hit(self, kind: str) -> None:
        self.clamps[kind] = time.monotonic()

    def on_cmd(self, m: dict) -> None:
        now = time.monotonic()
        self.cmd_count += 1
        self.cmd_times.append(now)
        self.last_cmd = m
        if isinstance(m.get("ts_ms"), (int, float)):
            self.one_way_ms.append(time.time() * 1000 - m["ts_ms"])
        if self.stopped:
            return
        clutch = bool(m.get("clutch"))
        if clutch and (not self.clutch or self.watchdog):
            # clutch-down (or resume after a watchdog hold): anchor at the current pose
            self.anchor_pos = self.pos.copy()
            self.anchor_q = self.quat.copy()
            self.watchdog = False
        self.clutch = clutch
        self.last_cmd_at = now
        self.speed_scale = float(np.clip(m.get("speed_scale", 0.25), 0.01, 1.0))
        g = float(m.get("gripper", self.gripper_cmd))
        if g < 0 or g > 1:
            self.hit("clamped_gripper")
        self.gripper_cmd = float(np.clip(g, 0, 1))
        if clutch:
            self.control_source = "human"
            t = self.anchor_pos + np.array(m.get("d_pos", [0, 0, 0]), dtype=float)
            tc = np.clip(t, BOX_LO, BOX_HI)
            if not np.allclose(t, tc):
                self.hit("clamped_workspace")
            self.target = tc
            self.target_q = qmul(q_from_rotvec(np.array(m.get("d_rot", [0, 0, 0]), dtype=float)), self.anchor_q)
        else:
            self.hold()

    def hold(self) -> None:
        self.target = self.pos.copy()
        self.target_q = self.quat.copy()

    def stop(self, why: str) -> None:
        self.stopped = True
        self.clutch = False
        self.hold()
        since = time.monotonic() - self.last_cmd_at if self.last_cmd_at else None
        self.stop_log.append({"at": time.time(), "why": why, "s_since_last_cmd": since})
        print(f"[MOCK] STOP latched ({why}); last cmd {since and round(since * 1000)} ms before", flush=True)

    def step(self, dt: float) -> None:
        now = time.monotonic()
        if self.clutch and not self.watchdog and now - self.last_cmd_at > WATCHDOG_S:
            self.watchdog = True
            self.hit("watchdog_hold")
            self.hold()
            print("[MOCK] watchdog hold: no cmd for 200 ms while the clutch was down", flush=True)
        if self.stopped:
            return
        d = self.target - self.pos
        n = float(np.linalg.norm(d))
        vmax = MAX_LIN * self.speed_scale * dt
        if n > vmax + 1e-9:
            self.pos = self.pos + d * (vmax / n)
            if n > vmax * 3:
                self.hit("clamped_speed")
        else:
            self.pos = self.target.copy()
        self.quat = self.quat + (self.target_q - self.quat) * min(1.0, dt * 8)
        self.quat /= np.linalg.norm(self.quat)
        self.gripper += (self.gripper_cmd - self.gripper) * min(1.0, dt * 6)
        if not self.clutch and self.control_source == "human" and now - self.last_cmd_at > 2:
            self.control_source = "idle"

    def state(self) -> dict:
        now = time.monotonic()
        return {
            "t": "state",
            "mock": True,
            "pose": {"pos": [round(float(v), 4) for v in self.pos], "quat": [round(float(v), 4) for v in self.quat]},
            "joints": [0.0] * 6,
            "gripper": round(self.gripper, 3),
            "clamps": {k: True for k, at in self.clamps.items() if now - at < 1.0},
            "control_source": self.control_source,
            "recording": self.recording,
            "stopped": self.stopped,
            "watchdog": self.watchdog,
            "speed_scale": self.speed_scale,
            "clutch": self.clutch,
        }


class Cams:
    def __init__(self) -> None:
        self.frozen_until = 0.0
        self.delay_ms = 0.0
        self.seq = {"top": 0, "wrist": 0}

    def render(self, cam: str, r: Robot) -> bytes:
        img = np.full((300, 400, 3), (38, 34, 30), np.uint8)
        if cam == "top":
            cv2.rectangle(img, (40, 30), (360, 270), (90, 90, 90), 1)
            px = int(200 - (r.pos[1] / 0.30) * 160)
            py = int(270 - ((r.pos[0] - BOX_LO[0]) / (BOX_HI[0] - BOX_LO[0])) * 240)
            rad = int(8 + r.pos[2] * 40)
            cv2.circle(img, (px, py), rad, (0, 180, 240) if r.clutch else (200, 200, 200), -1)
            cv2.putText(img, "TOP (sim)", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
        else:
            open_px = int(10 + r.gripper * 80)
            cv2.rectangle(img, (200 - open_px - 20, 120), (200 - open_px, 240), (180, 180, 180), -1)
            cv2.rectangle(img, (200 + open_px, 120), (200 + open_px + 20, 240), (180, 180, 180), -1)
            cv2.putText(img, f"WRIST (sim) z={r.pos[2] * 100:.1f}cm", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)
        cv2.putText(img, time.strftime("%H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}", (8, 290),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (160, 160, 160), 1)
        if r.stopped:
            cv2.putText(img, "STOPPED", (130, 160), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (40, 40, 220), 3)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return jpg.tobytes()

    def frame(self, cam: str, r: Robot) -> bytes:
        self.seq[cam] += 1
        cap = time.monotonic_ns() - int(self.delay_ms * 1e6)
        name = cam.encode()
        head = struct.pack(">BB", 1, len(name)) + name + struct.pack(">QQI", cap, time.monotonic_ns(), self.seq[cam])
        return head + self.render(cam, r)


ROBOT = Robot()
CAMS = Cams()


async def auth(ws: ServerConnection) -> bool:
    try:
        raw = await asyncio.wait_for(ws.recv(), 5)
        m = json.loads(raw)
    except Exception:
        await ws.close(4003, "no auth")
        return False
    if (m.get("type") == "auth" or m.get("t") == "auth") and m.get("token"):
        return True
    await ws.close(4003, "bad token")
    return False


async def cameras(ws: ServerConnection) -> None:
    if not await auth(ws):
        return
    await ws.send(json.dumps({"type": "hello", "cams": ["top", "wrist"], "exp": time.time() + 3600, "mock": True}))
    inflight = {"top": 0, "wrist": 0}

    async def reader():
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong", "t": m.get("t"), "server_ns": time.monotonic_ns()}))
            elif m.get("type") == "ack" and m.get("cam") in inflight:
                inflight[m["cam"]] = max(0, inflight[m["cam"]] - 1)
            elif m.get("type") == "auth":
                await ws.send(json.dumps({"type": "auth_ok", "exp": time.time() + 3600}))

    rt = asyncio.create_task(reader())
    try:
        while not rt.done():
            if time.monotonic() >= CAMS.frozen_until:
                for cam in ("top", "wrist"):
                    if inflight[cam] < 2:
                        inflight[cam] += 1
                        await ws.send(CAMS.frame(cam, ROBOT))
            await asyncio.sleep(1 / 15)
    except ConnectionClosed:
        pass
    finally:
        rt.cancel()


async def control(ws: ServerConnection) -> None:
    if not await auth(ws):
        return
    print("[MOCK] control client connected", flush=True)

    async def pusher():
        while True:
            await ws.send(json.dumps(ROBOT.state()))
            await asyncio.sleep(0.1)

    pt = asyncio.create_task(pusher())
    try:
        async for raw in ws:
            m = json.loads(raw)
            t = m.get("t") or m.get("type")
            if t == "cmd":
                ROBOT.on_cmd(m)
                await ws.send(json.dumps({"t": "ack", "seq": m.get("seq")}))
            elif t == "stop":
                if not ROBOT.stopped:
                    ROBOT.stop("browser STOP")
            elif t == "record_start":
                ROBOT.recording = True
            elif t == "record_stop":
                ROBOT.recording = False
            elif t in ("takeover", "handback"):
                ROBOT.control_source = "human" if t == "takeover" else "policy"
            elif t == "auth":
                pass
    except ConnectionClosed:
        pass
    finally:
        pt.cancel()
        ROBOT.clutch = False
        ROBOT.hold()
        print("[MOCK] control client left; holding", flush=True)


async def handler(ws: ServerConnection) -> None:
    path = urlsplit(ws.request.path).path
    if path == "/ws/cameras":
        await cameras(ws)
    elif path == "/ws/control":
        await control(ws)
    else:
        await ws.close(4004, "unknown path")


def process_request(conn: ServerConnection, request):
    u = urlsplit(request.path)
    if u.path.startswith("/ws/"):
        return None
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if u.path == "/debug/freeze":
        CAMS.frozen_until = time.monotonic() + float(q.get("s", 3))
        body = {"frozen_s": float(q.get("s", 3))}
    elif u.path == "/debug/delay":
        CAMS.delay_ms = float(q.get("ms", 0))
        body = {"delay_ms": CAMS.delay_ms}
    elif u.path == "/debug/robot_stop":
        ROBOT.stop("debug at the arm")
        body = {"stopped": True}
    elif u.path == "/debug/clear":
        ROBOT.stopped = False
        body = {"stopped": False}
    elif u.path in ("/debug/stats", "/healthz"):
        now = time.monotonic()
        ow = sorted(ROBOT.one_way_ms)
        body = {
            "mock": True,
            "cmd_hz_last_s": sum(1 for t in ROBOT.cmd_times if now - t < 1.0),
            "cmd_count": ROBOT.cmd_count,
            "last_cmd": ROBOT.last_cmd,
            "s_since_last_cmd": (now - ROBOT.last_cmd_at) if ROBOT.last_cmd_at else None,
            "stop_log": ROBOT.stop_log,
            "one_way_ms_p50": ow[len(ow) // 2] if ow else None,
            "state": ROBOT.state(),
        }
    else:
        return conn.respond(404, "not found\n")
    r = conn.respond(200, json.dumps(body) + "\n")
    r.headers["Content-Type"] = "application/json"
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r


async def sim_loop() -> None:
    last = time.monotonic()
    last_print = last
    while True:
        await asyncio.sleep(0.01)
        now = time.monotonic()
        ROBOT.step(now - last)
        last = now
        while ROBOT.cmd_times and now - ROBOT.cmd_times[0] > 5:
            ROBOT.cmd_times.popleft()
        if now - last_print >= 1.0:
            last_print = now
            hz = sum(1 for t in ROBOT.cmd_times if now - t < 1.0)
            if hz or ROBOT.clutch:
                print(f"[MOCK] cmd {hz} Hz clutch={ROBOT.clutch} pos={np.round(ROBOT.pos, 3).tolist()} "
                      f"grip={ROBOT.gripper:.2f} scale={ROBOT.speed_scale:.2f}", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8156)
    a = ap.parse_args()
    async with serve(handler, a.host, a.port, process_request=process_request, max_size=2**20):
        print(f"[MOCK] tdm-56 mock robot on ws://{a.host}:{a.port}/ws/cameras + /ws/control", flush=True)
        await sim_loop()


if __name__ == "__main__":
    asyncio.run(main())
