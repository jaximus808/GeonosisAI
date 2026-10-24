"""TDM-55: camera stream from the Jetson to the browser (JPEG over WebSocket, through a tunnel).

Transport ``jpeg-ws/1`` (decision + revert plan: ``docs/decisions.md``, "TDM-55"):

* One WebSocket per viewer at ``/ws/cameras``. Each camera frame is one binary message: a small
  header (camera, capture time, camera seq) and the JPEG. Encoding happens once per frame on a
  thread per camera, right when the frame arrives (no polling delay), and only while somebody watches.
* **Ack pacing**: the server keeps at most ``max_in_flight`` (default 2) un-acked frames per camera
  per viewer and always sends the *newest* frame. A slow phone or a slow link gets fewer fps,
  never a growing backlog, so latency stays bounded. That is what makes it usable for teleop.
* **Clock sync**: ``ping``/``pong`` carries the server's ``time.monotonic_ns()`` (the clock the
  frames are stamped with), so the browser can show capture-to-screen latency per frame.
* **Auth**: nothing is sent before an ``auth`` message with a valid ``rdf_common.stream_token``
  for this robot (minted by the orchestrator for the current task claimant, ``POST /stream/token``).
  The token is short-lived (<= 60 s, capped at the claim's lease); the browser sends a fresh one at
  half-life, and the socket closes with code 4001 when it expires. Bad token: 4003.
* **Public URL**: a cloudflared quick tunnel (no account; outbound HTTPS only, so no NAT/TURN
  issues on the office network). ``--tunnel none`` for LAN only. Only this server is tunneled; the
  TDM-47 MJPEG debug server (``camera_stream.serve_mjpeg``) has no auth and must stay on 127.0.0.1.

Endpoints (one port, default 8091):

  ``GET /ws/cameras``                 WebSocket, protocol below
  ``GET /ws/control`` (+ ``/control``)  WebSocket, TDM-52 teleop control (``teleop.py``), when registered
  ``GET /snapshot/<cam>.jpg?token=``  one JPEG (the browser's fallback when WebSockets fail)
  ``GET /healthz``                    JSON: cameras, viewers, public URL (no frames, no auth)

Protocol (client -> server, JSON text)::

  {"type":"auth","token":"..."}                        first message (within 5 s), and every refresh
  {"type":"subscribe","cams":["top","wrist"],"fps":20}  optional; default every camera the token allows
  {"type":"ack","cam":"top","seq":123}                 after each frame is drawn
  {"type":"ping","t":<client ms>}

server -> client: JSON ``hello`` / ``pong`` {t, server_ns} / ``stats`` (1 Hz) / ``auth_ok`` {exp} /
``error`` {code}; binary frames::

  u8 version(=1) | u8 len(cam) | cam (ascii) | u64 capture_ns | u64 send_ns | u32 cam_seq | JPEG...
  (big-endian; capture_ns/send_ns are the server's monotonic clock)

Use from the command loop (TDM-49)::

    from rdf_robot_client.stream import start_stream
    stream = start_stream(rig, robot_id=1, robot_name="yam-office-1", tunnel="cloudflared")
    camera_config = {**camera_config, **stream.info()}   # -> robot.camera_config_json["stream"]
    stream.on_url(lambda info: ...)                     # tunnel restarted with a new URL
    stream.stop()

CLI (laptop or Jetson)::

    python -m rdf_robot_client.stream serve --synthetic --robot-id 1 [--tunnel cloudflared] [--announce]
    python -m rdf_robot_client.stream probe wss://<host>/ws/cameras --token <t> --seconds 10
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import re
import shutil
import statistics
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from rdf_common.stream_token import StreamClaims, StreamTokenError, verify

from .cameras import CameraRig, Frame

log = logging.getLogger("rdf_robot_client.stream")

TRANSPORT = "jpeg-ws/1"
WS_PATH = "/ws/cameras"
DEFAULT_PORT = 8091
HEADER = struct.Struct(">QQI")
CLOSE_EXPIRED, CLOSE_FORBIDDEN = 4001, 4003
AUTH_TIMEOUT_S = 5.0
TRYCLOUDFLARE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def pack_frame(cam: str, capture_ns: int, seq: int, jpeg: bytes, send_ns: int | None = None) -> bytes:
    name = cam.encode("ascii")
    return (bytes([1, len(name)]) + name
            + HEADER.pack(capture_ns, send_ns if send_ns is not None else time.monotonic_ns(), seq & 0xFFFFFFFF)
            + jpeg)


def unpack_frame(msg: bytes) -> tuple[str, int, int, int, bytes]:
    """-> (cam, capture_ns, send_ns, seq, jpeg). Used by ``probe`` and the tests."""
    if msg[0] != 1:
        raise ValueError(f"unknown frame version {msg[0]}")
    n = msg[1]
    cam = msg[2:2 + n].decode("ascii")
    cap, sent, seq = HEADER.unpack_from(msg, 2 + n)
    return cam, cap, sent, seq, bytes(msg[2 + n + HEADER.size:])


# ------------------------------------------------------------------------------------------ encoding

@dataclass(eq=False)
class EncodedFrame:
    cam: str
    seq: int
    capture_ns: int
    encoded_ns: int
    jpeg: bytes


class _Viewer:
    """Per-connection mailbox: newest encoded frame per camera (older ones are simply replaced)."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.latest: dict[str, EncodedFrame] = {}
        self.event = asyncio.Event()

    def offer(self, ef: EncodedFrame) -> None:  # called from encoder threads
        def _set() -> None:
            self.latest[ef.cam] = ef
            self.event.set()
        try:
            self.loop.call_soon_threadsafe(_set)
        except RuntimeError:  # loop closed
            pass


class _CamEncoder:
    """One thread per camera: JPEG-encode each new frame (throttled to ``fps``) while it has viewers."""

    def __init__(self, rig: CameraRig, cam: str, width: int, quality: int, fps: float):
        self.cam, self.width, self.quality = cam, width, quality
        self.period_ns = int(1e9 / fps) if fps > 0 else 0
        self.viewers: list[_Viewer] = []
        self._lock = threading.Lock()
        self._q = rig.subscribe_frames(cam, maxsize=1)
        self._stop = threading.Event()
        self.encode_ms: deque[float] = deque(maxlen=60)
        self.sizes: deque[int] = deque(maxlen=60)
        self.encoded = 0
        self._thread = threading.Thread(target=self._run, name=f"stream-enc-{cam}", daemon=True)
        self._thread.start()

    def add(self, v: _Viewer) -> None:
        with self._lock:
            self.viewers = [*self.viewers, v]

    def remove(self, v: _Viewer) -> None:
        with self._lock:
            self.viewers = [x for x in self.viewers if x is not v]

    def stop(self) -> None:
        self._stop.set()
        close = getattr(self._q, "close", None)
        if close:
            close()
        self._thread.join(2.0)

    def _run(self) -> None:
        due_ns = 0  # credit schedule: 30 fps camera -> 20 fps stream sends 2 of every 3 frames
        slack_ns = 17_000_000  # about half a camera frame at 30 fps
        while not self._stop.is_set():
            try:
                f: Frame = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            viewers = self.viewers
            if not viewers or f.t_ns + slack_ns < due_ns:
                continue
            due_ns = max(due_ns + self.period_ns, f.t_ns - self.period_ns)
            t0 = time.perf_counter()
            try:
                jpg = f.jpeg(self.width, self.quality)
            except Exception:
                log.exception("encode %s failed", self.cam)
                continue
            self.encode_ms.append((time.perf_counter() - t0) * 1e3)
            self.sizes.append(len(jpg))
            self.encoded += 1
            ef = EncodedFrame(self.cam, f.seq, f.t_ns, time.monotonic_ns(), jpg)
            for v in viewers:
                v.offer(ef)

    def stats(self) -> dict[str, Any]:
        return {"viewers": len(self.viewers), "encoded": self.encoded,
                "encode_ms_p50": round(statistics.median(self.encode_ms), 2) if self.encode_ms else None,
                "jpeg_kb_p50": round(statistics.median(self.sizes) / 1024, 1) if self.sizes else None}


# -------------------------------------------------------------------------------------------- server

@dataclass
class StreamConfig:
    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"
    width: int | None = None          # default: cameras.yaml stream.width (424)
    quality: int | None = None        # default: stream.jpeg_quality (70)
    fps: float = 20.0
    max_in_flight: int = 2
    robot_id: int | None = None       # SpacetimeDB robot id the token must name ...
    robot_name: str | None = None     # ... or this robot name
    allow_any_robot: bool = False     # dev only: accept tokens for any robot
    secret: Any = None                # default: RDF_STREAM_TOKEN_SECRET (env or .env)


class CameraStreamServer:
    def __init__(self, rig: CameraRig, cfg: StreamConfig):
        if not (cfg.robot_id or cfg.robot_name or cfg.allow_any_robot):
            raise ValueError("set robot_id or robot_name (the token must be scoped to this robot)")
        scfg = rig.config.stream or {}
        self.rig, self.cfg = rig, cfg
        self.width = int(cfg.width or scfg.get("width", 424))
        self.quality = int(cfg.quality or scfg.get("jpeg_quality", 70))
        if cfg.secret is None:
            from rdf_common.stream_token import load_secret
            cfg.secret = load_secret()
        self.encoders = {c: _CamEncoder(rig, c, self.width, self.quality, cfg.fps) for c in rig.names}
        self.public_url: str | None = None   # https://... (set by the tunnel)
        self.connections = 0
        self.denied = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._started = threading.Event()
        self._thread: threading.Thread | None = None
        self.port = cfg.port
        # TDM-52: extra WebSocket paths on the same port/tunnel/token, e.g. "/control" -> coroutine(server, ws)
        self.ws_routes: dict[str, Callable[["CameraStreamServer", ServerConnection], Any]] = {}

    def add_ws_route(self, path: str, handler: Callable[["CameraStreamServer", ServerConnection], Any]) -> None:
        """Serve another WebSocket path (TDM-52 ``/control``) on this server; ``handler(server, ws)`` does its own auth."""
        self.ws_routes[path] = handler

    # -- auth
    def check_token(self, token: str) -> StreamClaims:
        claims = verify(self.cfg.secret, token)
        if not self.cfg.allow_any_robot:
            ok = (self.cfg.robot_id and claims.robot_id == self.cfg.robot_id) or \
                 (self.cfg.robot_name and claims.robot_name == self.cfg.robot_name)
            if not ok:
                raise StreamTokenError("wrong_robot")
        return claims

    # -- HTTP side (healthz / snapshot)
    def _http(self, conn: ServerConnection, req: Request) -> Response | None:
        u = urlsplit(req.path)
        if u.path == WS_PATH or u.path in self.ws_routes:
            return None
        hdr = Headers({"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})
        if u.path == "/healthz":
            body = json.dumps(self.health()).encode()
            hdr["Content-Type"] = "application/json"
            return Response(200, "OK", hdr, body)
        m = re.fullmatch(r"/snapshot/([A-Za-z0-9_-]+)\.jpg", u.path)
        if m:
            cam = m.group(1)
            token = (parse_qs(u.query).get("token") or [""])[0]
            try:
                claims = self.check_token(token)
            except StreamTokenError as e:
                self.denied += 1
                return Response(403, "Forbidden", hdr, e.code.encode())
            if cam not in self.encoders or not claims.allows(cam):
                return Response(404, "Not Found", hdr, b"unknown camera")
            f = self.rig.latest(cam)
            if f is None:
                return Response(503, "Service Unavailable", hdr, b"no frame yet")
            hdr["Content-Type"] = "image/jpeg"
            hdr["X-Capture-Ns"] = str(f.t_ns)
            hdr["X-Server-Ns"] = str(time.monotonic_ns())
            hdr["Access-Control-Expose-Headers"] = "X-Capture-Ns, X-Server-Ns"
            return Response(200, "OK", hdr, f.jpeg(self.width, self.quality))
        return Response(404, "Not Found", hdr, b"not found")

    def health(self) -> dict[str, Any]:
        st = self.rig.stats()
        return {"ok": self.rig.healthy(), "transport": TRANSPORT, "cams": self.rig.names,
                "public_url": self.public_url, "connections": self.connections, "denied": self.denied,
                "width": self.width, "fps": self.cfg.fps,
                "encoders": {c: e.stats() for c, e in self.encoders.items()},
                "rig_fps": {c: v.get("fps") for c, v in (st.get("cameras") or {}).items()}}

    # -- WebSocket side
    async def _handler(self, ws: ServerConnection) -> None:
        route = self.ws_routes.get(urlsplit(ws.request.path).path) if ws.request else None
        if route is not None:
            await route(self, ws)
            return
        try:
            first = await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT_S)
            msg = json.loads(first) if isinstance(first, str) else {}
            if msg.get("type") != "auth":
                raise StreamTokenError("auth_required")
            claims = self.check_token(str(msg.get("token", "")))
        except (asyncio.TimeoutError, json.JSONDecodeError, StreamTokenError) as e:
            self.denied += 1
            code = e.code if isinstance(e, StreamTokenError) else "auth_required"
            log.info("stream: denied %s (%s)", ws.remote_address, code)
            await self._send_json(ws, {"type": "error", "code": code})
            await ws.close(CLOSE_EXPIRED if code == "expired" else CLOSE_FORBIDDEN, code)
            return
        except ConnectionClosed:
            return
        await self._session(ws, claims)

    @staticmethod
    async def _send_json(ws: ServerConnection, obj: dict) -> None:
        try:
            await ws.send(json.dumps(obj))
        except ConnectionClosed:
            pass

    async def _session(self, ws: ServerConnection, claims: StreamClaims) -> None:
        loop = asyncio.get_running_loop()
        viewer = _Viewer(loop)
        state = {"claims": claims, "cams": [c for c in self.rig.names if claims.allows(c)],
                 "min_gap_ns": 0}
        in_flight = {c: 0 for c in self.rig.names}
        sent_seq = {c: -1 for c in self.rig.names}
        sent_count = {c: 0 for c in self.rig.names}
        sent_bytes = {c: 0 for c in self.rig.names}
        last_sent_ns = {c: 0 for c in self.rig.names}
        last_ack = {c: time.monotonic() for c in self.rig.names}
        self.connections += 1
        log.info("stream: viewer %s sub=%s task=%s", ws.remote_address, claims.sub, claims.task_id)

        def attach(cams: list[str]) -> None:
            for c, enc in self.encoders.items():
                (enc.add if c in cams else enc.remove)(viewer)

        attach(state["cams"])
        await self._send_json(ws, {"type": "hello", "transport": TRANSPORT, "cams": state["cams"],
                                   "robot_id": claims.robot_id, "task_id": claims.task_id, "exp": claims.exp,
                                   "max_in_flight": self.cfg.max_in_flight, "width": self.width,
                                   "fps": self.cfg.fps, "server_ns": time.monotonic_ns()})

        async def sender() -> None:
            while True:
                await viewer.event.wait()
                viewer.event.clear()
                for cam in list(state["cams"]):
                    ef = viewer.latest.get(cam)
                    if ef is None or ef.seq == sent_seq[cam] or in_flight[cam] >= self.cfg.max_in_flight:
                        continue
                    if state["min_gap_ns"] and ef.capture_ns - last_sent_ns[cam] < state["min_gap_ns"]:
                        continue
                    sent_seq[cam], last_sent_ns[cam] = ef.seq, ef.capture_ns
                    in_flight[cam] += 1
                    sent_count[cam] += 1
                    sent_bytes[cam] += len(ef.jpeg)
                    await ws.send(pack_frame(cam, ef.capture_ns, ef.seq, ef.jpeg))

        async def receiver() -> None:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                try:
                    m = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                t = m.get("type")
                if t == "ack":
                    cam = m.get("cam")
                    if cam in in_flight and in_flight[cam] > 0:
                        in_flight[cam] -= 1
                        last_ack[cam] = time.monotonic()
                        viewer.event.set()  # a newer frame may be waiting
                elif t == "ping":
                    await self._send_json(ws, {"type": "pong", "t": m.get("t"), "server_ns": time.monotonic_ns()})
                elif t == "auth":
                    try:
                        c = self.check_token(str(m.get("token", "")))
                    except StreamTokenError as e:
                        await self._send_json(ws, {"type": "error", "code": e.code})
                        continue  # keep the old token until it expires
                    state["claims"] = c
                    await self._send_json(ws, {"type": "auth_ok", "exp": c.exp})
                elif t == "subscribe":
                    want = [x for x in (m.get("cams") or state["cams"]) if x in self.encoders
                            and state["claims"].allows(x)]
                    state["cams"] = want
                    fps = float(m.get("fps") or 0)
                    state["min_gap_ns"] = int(1e9 / fps * 0.9) if fps > 0 else 0
                    attach(want)
                    viewer.event.set()

        async def watchdog() -> None:
            last = {c: (0, 0) for c in self.rig.names}
            while True:
                await asyncio.sleep(1.0)
                if time.time() > state["claims"].exp + 2.0:
                    await self._send_json(ws, {"type": "error", "code": "expired"})
                    await ws.close(CLOSE_EXPIRED, "expired")
                    return
                for c in state["cams"]:  # a client that stopped acking (e.g. decode error) gets unstuck
                    if in_flight[c] >= self.cfg.max_in_flight and time.monotonic() - last_ack[c] > 3.0:
                        in_flight[c], last_ack[c] = 0, time.monotonic()
                        viewer.event.set()
                cams = {}
                for c in state["cams"]:
                    n0, b0 = last[c]
                    cams[c] = {"fps": sent_count[c] - n0, "kbps": round((sent_bytes[c] - b0) * 8 / 1000),
                               "in_flight": in_flight[c]}
                    last[c] = (sent_count[c], sent_bytes[c])
                await self._send_json(ws, {"type": "stats", "server_ns": time.monotonic_ns(), "cams": cams,
                                           "exp": state["claims"].exp})

        tasks = [asyncio.create_task(x()) for x in (sender, receiver, watchdog)]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except ConnectionClosed:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            attach([])
            self.connections -= 1
            log.info("stream: viewer %s left", ws.remote_address)

    # -- lifecycle (own event loop on a daemon thread, so sync callers like TDM-49 can use it)
    def start(self) -> "CameraStreamServer":
        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def main() -> None:
                self._server = await serve(self._handler, self.cfg.host, self.cfg.port, process_request=self._http,
                                           compression=None, max_size=64 * 1024, ping_interval=10,
                                           ping_timeout=20, server_header="rdf-robot-client")
                self.port = self._server.sockets[0].getsockname()[1]
                self._started.set()
                await self._server.wait_closed()

            try:
                self._loop.run_until_complete(main())
            finally:
                self._started.set()

        self._thread = threading.Thread(target=run, name="stream-ws", daemon=True)
        self._thread.start()
        if not self._started.wait(10) or self._server is None:
            raise RuntimeError(f"stream server failed to start on {self.cfg.host}:{self.cfg.port}")
        log.info("stream: listening on %s:%s", self.cfg.host, self.port)
        return self

    def stop(self) -> None:
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread:
            self._thread.join(5)
        for e in self.encoders.values():
            e.stop()


# -------------------------------------------------------------------------------------------- tunnel

class QuickTunnel:
    """``cloudflared tunnel --url http://127.0.0.1:<port>`` (no account). Restarts it if it exits.

    The URL changes on every restart; ``on_url(url)`` callbacks fire once cloudflared logs a
    registered edge connection. We deliberately do NOT poll the public URL from this machine: a lookup
    before the quick-tunnel DNS record exists gets NXDOMAIN cached by the local resolver (seen on
    macOS + Tailscale DNS) and the URL then looks dead here for minutes while it works everywhere else.
    """

    def __init__(self, port: int, binary: str | None = None, protocol: str = "auto"):
        self.port = port
        self.binary = binary or os.environ.get("CLOUDFLARED_BIN") or shutil.which("cloudflared")
        if not self.binary:
            raise RuntimeError("cloudflared not found: on the Jetson see docs/cameras-jetson-checklist.md "
                               "section 7, on a Mac `brew install cloudflared`, or run with --tunnel none")
        self.protocol = protocol
        self.url: str | None = None
        self._cbs: list[Callable[[str], None]] = []
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stream-tunnel", daemon=True)

    def on_url(self, fn: Callable[[str], None]) -> None:
        self._cbs.append(fn)

    def start(self, wait_s: float = 30.0) -> str | None:
        self._thread.start()
        self._ready.wait(wait_s)
        return self.url

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            cmd = [self.binary, "tunnel", "--no-autoupdate", "--protocol", self.protocol,
                   "--url", f"http://127.0.0.1:{self.port}"]
            log.info("tunnel: starting cloudflared quick tunnel -> :%s", self.port)
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            assert self._proc.stderr is not None
            found, live = None, False
            for line in self._proc.stderr:
                if found is None and (m := TRYCLOUDFLARE.search(line)):
                    found = m.group(0)
                if found and not live and "Registered tunnel connection" in line:
                    live = True
                    self.url = found
                    log.info("tunnel: %s is live (%s)", found, line.split("location=")[-1].split()[0]
                             if "location=" in line else "registered")
                    self._ready.set()
                    backoff = 2.0
                    for fn in self._cbs:
                        try:
                            fn(found)
                        except Exception:
                            log.exception("tunnel url callback failed")
                elif " ERR " in line:
                    log.warning("cloudflared: %s", line.strip()[:300])
            self._proc.wait()
            if self._stop.is_set():
                return
            log.warning("tunnel: cloudflared exited (%s); restarting in %.0fs", self._proc.returncode, backoff)
            self.url = None
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)


# --------------------------------------------------------------------------------------- public API

class StreamHandle:
    def __init__(self, server: CameraStreamServer, tunnel: QuickTunnel | None, lan_host: str | None):
        self.server, self.tunnel, self.lan_host = server, tunnel, lan_host
        self._cbs: list[Callable[[dict], None]] = []
        if tunnel:
            tunnel.on_url(self._on_tunnel_url)

    def _on_tunnel_url(self, url: str) -> None:
        self.server.public_url = url
        info = self.info()
        for fn in self._cbs:
            try:
                fn(info)
            except Exception:
                log.exception("stream on_url callback failed")

    def on_url(self, fn: Callable[[dict], None]) -> None:
        """``fn(info)`` whenever the public URL changes (tunnel restart). Re-publish ``info`` then."""
        self._cbs.append(fn)

    @property
    def http_url(self) -> str | None:
        if self.server.public_url:
            return self.server.public_url
        if self.lan_host:
            return f"http://{self.lan_host}:{self.server.port}"
        return None

    def info(self) -> dict[str, Any]:
        """``{"stream": {...}}`` to merge into ``robot.camera_config_json`` (the orchestrator reads it)."""
        http = self.http_url
        ws = None if http is None else re.sub(r"^http", "ws", http) + WS_PATH
        info = {"transport": TRANSPORT, "url": ws, "http": http, "cams": self.server.rig.names,
                "width": self.server.width, "fps": self.server.cfg.fps,
                "tunnel": "cloudflared" if self.tunnel else "none", "updated_at": int(time.time())}
        ctl = next((p for p in ("/ws/control", "/control") if p in self.server.ws_routes), None)
        if ctl:  # TDM-52 teleop control socket (same token)
            info["control_url"] = None if http is None else re.sub(r"^http", "ws", http) + ctl
        return {"stream": info}

    def stop(self) -> None:
        if self.tunnel:
            self.tunnel.stop()
        self.server.stop()


def start_stream(rig: CameraRig, *, robot_id: int | None = None, robot_name: str | None = None,
                 port: int = DEFAULT_PORT, host: str | None = None, tunnel: str = "cloudflared",
                 wait_url_s: float = 30.0, lan_host: str | None = None, **cfg: Any) -> StreamHandle:
    """Start the camera stream server (and the tunnel). ``rig`` must already be started.

    ``tunnel="cloudflared"`` binds to 127.0.0.1 and blocks up to ``wait_url_s`` for the public URL
    (``info()["stream"]["url"]`` is None if it did not come up yet; ``on_url`` fires later).
    ``tunnel="none"`` binds ``host`` (default 0.0.0.0) for LAN viewers at ``ws://<lan_host>:<port>``.
    """
    if tunnel not in ("cloudflared", "none"):
        raise ValueError(f"tunnel must be cloudflared|none, got {tunnel!r}")
    bind = host or ("127.0.0.1" if tunnel == "cloudflared" else "0.0.0.0")
    server = CameraStreamServer(rig, StreamConfig(port=port, host=bind, robot_id=robot_id,
                                                  robot_name=robot_name, **cfg)).start()
    qt = QuickTunnel(server.port) if tunnel == "cloudflared" else None
    handle = StreamHandle(server, qt, lan_host or (None if qt else _lan_ip()))
    if qt:
        qt.start(wait_url_s)
    return handle


def _lan_ip() -> str:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def announce(info: dict[str, Any], robot_name: str) -> None:
    """Merge ``info`` into the robot row's ``camera_config_json`` via ``register_robot`` (upsert by name).

    For the standalone CLI. Inside the command loop, TDM-49 owns registration: merge ``info()``
    into the ``camera_config`` it registers with instead.
    """
    from rdf_common.settings import SpacetimeSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    c = SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "robot-client"))
    safe = robot_name.replace("'", "''")
    rows = c.sql(f"SELECT * FROM robot WHERE name = '{safe}'")
    if not rows:
        raise RuntimeError(f"no robot named {robot_name!r}; register it first (rdf-robot-client)")
    r = rows[0]
    try:
        cc = json.loads(r["camera_config_json"] or "{}")
    except json.JSONDecodeError:
        cc = {}
    cc.update(info)
    c.call_reducer("register_robot", r["name"], r["embodiment"], r["adapter_version"], json.dumps(cc))
    log.info("stream: announced %s on robot %s (id %s)", info["stream"]["url"], robot_name, r["id"])


# ---------------------------------------------------------------------------------------------- probe

async def probe(url: str, token: str, seconds: float = 10.0, cams: list[str] | None = None,
                max_in_flight_ack: bool = True, connect_ip: str | None = None) -> dict[str, Any]:
    """Connect like the browser, sync clocks, and report capture->receive latency per camera.

    ``connect_ip`` pins the TCP connection to that IP (TLS SNI/Host stay the URL's host); handy when
    this machine's resolver cached NXDOMAIN for a fresh quick-tunnel name (see ``QuickTunnel``).
    """
    import websockets

    lat: dict[str, list[float]] = {}
    sizes: dict[str, list[int]] = {}
    offsets: list[tuple[float, float]] = []  # (rtt_ms, offset_ms) where offset = server_ms - client_ms
    extra: dict[str, Any] = {}
    if connect_ip:
        u = urlsplit(url)
        extra = {"host": connect_ip, "port": u.port or (443 if u.scheme == "wss" else 80)}
        if u.scheme == "wss":
            extra["server_hostname"] = u.hostname
    async with websockets.connect(url, max_size=8 * 1024 * 1024, compression=None,
                                  open_timeout=20, **extra) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        hello = json.loads(await ws.recv())
        if hello.get("type") != "hello":
            return {"error": hello}
        if cams:
            await ws.send(json.dumps({"type": "subscribe", "cams": cams}))

        def now_ms() -> float:
            return time.perf_counter_ns() / 1e6

        async def pinger() -> None:
            while True:
                await ws.send(json.dumps({"type": "ping", "t": now_ms()}))
                await asyncio.sleep(0.5)

        pt = asyncio.create_task(pinger())
        t_end = time.monotonic() + seconds
        try:
            while time.monotonic() < t_end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, t_end - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, str):
                    m = json.loads(msg)
                    if m.get("type") == "pong":
                        t1 = now_ms()
                        rtt = t1 - m["t"]
                        offsets.append((rtt, m["server_ns"] / 1e6 - (m["t"] + t1) / 2))
                    elif m.get("type") == "error":
                        return {"error": m}
                    continue
                cam, cap, _sent, seq, jpg = unpack_frame(msg)
                recv = now_ms()
                if offsets:
                    off = min(offsets)[1]  # min-RTT sample is the most accurate
                    lat.setdefault(cam, []).append(recv - (cap / 1e6 - off))
                    sizes.setdefault(cam, []).append(len(jpg))
                if max_in_flight_ack:
                    await ws.send(json.dumps({"type": "ack", "cam": cam, "seq": seq}))
        finally:
            pt.cancel()

    def pct(xs: list[float], p: float) -> float:
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1)

    rtts = [r for r, _ in offsets]
    return {"url": url, "seconds": seconds, "rtt_ms_min": round(min(rtts), 1) if rtts else None,
            "rtt_ms_p50": round(statistics.median(rtts), 1) if rtts else None,
            "cams": {c: {"frames": len(v), "fps": round(len(v) / seconds, 1), "latency_ms_p50": pct(v, 0.5),
                         "latency_ms_p95": pct(v, 0.95), "jpeg_kb_p50": round(statistics.median(sizes[c]) / 1024, 1)}
                     for c, v in lat.items() if v}}


# ------------------------------------------------------------------------------------------------ CLI

def _cmd_serve(a: argparse.Namespace) -> int:
    from .cameras import CameraRig

    rig = CameraRig.from_config(a.config, synthetic=a.synthetic, webcam=a.webcam).start()
    if not rig.wait_ready(10):
        print("[stream] cameras not ready after 10 s; check `python -m rdf_robot_client.cameras check`",
              file=sys.stderr)
    h = start_stream(rig, robot_id=a.robot_id, robot_name=a.robot_name, port=a.port, host=a.host,
                     tunnel=a.tunnel, width=a.width, quality=a.quality, fps=a.fps,
                     allow_any_robot=a.any_robot, max_in_flight=a.in_flight)
    if a.announce:
        if not a.robot_name:
            print("[stream] --announce needs --robot-name", file=sys.stderr)
            return 2
        h.on_url(lambda info: announce(info, a.robot_name))
        if h.info()["stream"]["url"]:
            announce(h.info(), a.robot_name)
    print(json.dumps(h.info(), indent=2), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        h.stop()
        rig.stop()
    return 0


def _cmd_probe(a: argparse.Namespace) -> int:
    r = asyncio.run(probe(a.url, a.token, a.seconds, [c for c in a.cams.split(",") if c] or None,
                          connect_ip=a.connect_ip))
    print(json.dumps(r, indent=2))
    return 1 if "error" in r else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rdf_robot_client.stream", description="TDM-55 camera stream")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="camera stream server (+ cloudflared quick tunnel)")
    s.add_argument("--config", help="camera YAML (default config/cameras.yaml)")
    s.add_argument("--synthetic", action="store_true")
    s.add_argument("--webcam", type=int, default=None)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--host", default=None, help="bind address (default 127.0.0.1 with a tunnel, else 0.0.0.0)")
    s.add_argument("--tunnel", choices=["cloudflared", "none"], default="cloudflared")
    s.add_argument("--robot-id", type=int, default=None, help="SpacetimeDB robot id tokens must name")
    s.add_argument("--robot-name", default=None, help="robot name tokens may name instead")
    s.add_argument("--any-robot", action="store_true", help="dev only: accept a token for any robot")
    s.add_argument("--announce", action="store_true", help="write the URL into robot.camera_config_json")
    s.add_argument("--width", type=int, default=None)
    s.add_argument("--quality", type=int, default=None)
    s.add_argument("--fps", type=float, default=20.0)
    s.add_argument("--in-flight", type=int, default=StreamConfig.max_in_flight,
                   help="un-acked frames per camera per viewer (higher = more fps on high-RTT links)")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=_cmd_serve)
    p = sub.add_parser("probe", help="measure capture->receive latency like the browser does")
    p.add_argument("url")
    p.add_argument("--token", required=True)
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--cams", default="")
    p.add_argument("--connect-ip", default=None, help="pin the connection to this IP (keeps TLS SNI)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=_cmd_probe)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
