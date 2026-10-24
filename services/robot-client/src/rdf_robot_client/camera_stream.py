"""TDM-47: server-side camera feed for the browser (MJPEG). TDM-55 owns browser streaming end to end.

``MjpegFeed`` turns a running ``CameraRig`` into JPEG streams. It encodes on its own thread, only for
cameras somebody is watching, at ``stream.fps`` (default 15) and ``stream.width`` (default 424 px),
so streaming never slows capture. Each stream is ``top``, ``wrist`` or ``both`` (the latest synced
pair side by side).

Ways to use it:

* stdlib server, no extra deps (also ``python -m rdf_robot_client.cameras serve``)::

      srv = serve_mjpeg(MjpegFeed(rig), host="0.0.0.0", port=8090)
      #   GET /                      tiny page with every stream
      #   GET /stream/<cam>.mjpg     multipart/x-mixed-replace; boundary=frame  (<img src=...> works)
      #   GET /snapshot/<cam>.jpg    one JPEG
      #   GET /stats                 rig.stats() as JSON
      srv.shutdown()

* inside another web app (FastAPI / Starlette)::

      StreamingResponse(feed.iter_multipart("wrist"), media_type=MJPEG_CONTENT_TYPE)

* raw JPEG bytes for a WebSocket or a relay: ``q = feed.subscribe("top")`` then
  ``t_ns, seq, jpeg = q.get(timeout=1)``; ``feed.unsubscribe("top", q)`` when done.

* WebRTC (not built here): an aiortc ``VideoStreamTrack`` whose ``recv()`` reads
  ``rig.subscribe_frames("top")`` and wraps ``frame.rgb()`` in an ``av.VideoFrame``.

Every multipart part carries ``X-Timestamp-Ns`` (monotonic capture time) and ``X-Seq`` headers.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import cv2
import numpy as np

from .cameras import CameraRig, LatestQueue

log = logging.getLogger("rdf_robot_client.camera_stream")

BOUNDARY = "frame"
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"
BOTH = "both"


class MjpegFeed:
    def __init__(self, rig: CameraRig, width: int | None = None, quality: int | None = None,
                 fps: float | None = None):
        cfg = rig.config.stream or {}
        self.rig = rig
        self.width = int(width or cfg.get("width", 424))
        self.quality = int(quality or cfg.get("jpeg_quality", 70))
        self.fps = float(fps or cfg.get("fps", 15))
        self.streams = [*rig.names, BOTH]
        self._subs: dict[str, list[LatestQueue]] = {n: [] for n in self.streams}
        self._last: dict[str, tuple[int, int, bytes]] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="cam-mjpeg", daemon=True)
        self._thread.start()

    # -- subscriptions
    def subscribe(self, stream: str) -> LatestQueue:
        if stream not in self._subs:
            raise KeyError(f"unknown stream {stream!r}; have {self.streams}")
        q = LatestQueue(1)
        with self._lock:
            self._subs[stream] = [*self._subs[stream], q]
        return q

    def unsubscribe(self, stream: str, q: LatestQueue) -> None:
        with self._lock:
            self._subs[stream] = [x for x in self._subs.get(stream, []) if x is not q]

    def viewers(self) -> dict[str, int]:
        return {n: len(v) for n, v in self._subs.items()}

    def close(self) -> None:
        self._closed.set()
        self._thread.join(2.0)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    # -- encoding
    def _encode(self, stream: str) -> tuple[int, int, bytes] | None:
        if stream == BOTH:
            s = self.rig.latest_synced()
            if s is None:
                return None
            tiles = []
            for name in self.rig.names:
                f = s.frames.get(name)
                if f is None:
                    continue
                h = round(f.image.shape[0] * self.width / f.image.shape[1])
                tiles.append(cv2.resize(f.image, (self.width, h), interpolation=cv2.INTER_AREA))
            if not tiles:
                return None
            hmax = max(t.shape[0] for t in tiles)
            tiles = [np.pad(t, ((0, hmax - t.shape[0]), (0, 0), (0, 0))) for t in tiles]
            ok, buf = cv2.imencode(".jpg", np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, self.quality])
            return (s.t_ns, s.seq, buf.tobytes()) if ok else None
        f = self.rig.latest(stream)
        if f is None:
            return None
        return f.t_ns, f.seq, f.jpeg(self.width, self.quality)

    def snapshot(self, stream: str) -> bytes | None:
        last = self._last.get(stream)
        if last and time.monotonic_ns() - last[0] < 500_000_000:
            return last[2]
        enc = self._encode(stream)
        return enc[2] if enc else None

    def _loop(self) -> None:
        period = 1.0 / self.fps
        sent: dict[str, int] = {}
        nxt = time.monotonic()
        while not self._closed.is_set():
            nxt += period
            for stream, subs in list(self._subs.items()):
                if not subs:
                    continue
                try:
                    enc = self._encode(stream)
                except Exception:
                    log.exception("encode %s failed", stream)
                    continue
                if enc is None or sent.get(stream) == enc[1]:
                    continue
                sent[stream] = enc[1]
                self._last[stream] = enc
                for q in subs:
                    q.put(enc)
            delay = nxt - time.monotonic()
            if delay > 0:
                self._closed.wait(delay)
            else:
                nxt = time.monotonic()

    # -- multipart
    def iter_multipart(self, stream: str, idle_timeout: float = 2.0) -> Iterator[bytes]:
        """Yields multipart MJPEG parts until the feed closes or the consumer stops iterating."""
        q = self.subscribe(stream)
        try:
            while not self.closed:
                try:
                    t_ns, seq, jpg = q.get(timeout=idle_timeout)
                except queue.Empty:
                    continue
                yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n"
                       f"X-Timestamp-Ns: {t_ns}\r\nX-Seq: {seq}\r\n\r\n").encode() + jpg + b"\r\n"
        finally:
            self.unsubscribe(stream, q)


def _index_html(feed: MjpegFeed) -> bytes:
    imgs = "".join(
        f'<figure><img src="/stream/{n}.mjpg" alt="{n}"><figcaption>{n}</figcaption></figure>'
        for n in feed.rig.names)
    return (f"<!doctype html><meta charset=utf-8><title>robot cameras</title>"
            f"<style>body{{background:#111;color:#ddd;font:14px sans-serif;margin:16px}}"
            f"figure{{display:inline-block;margin:8px}}img{{max-width:100%;background:#000}}</style>"
            f"<h3>robot-client cameras (TDM-47 MJPEG)</h3>{imgs}"
            f'<p><a style="color:#9cf" href="/stats">/stats</a> · <a style="color:#9cf" '
            f'href="/stream/both.mjpg">/stream/both.mjpg</a></p>').encode()


def make_handler(feed: MjpegFeed) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt: str, *args) -> None:  # quiet by default
            log.debug("%s " + fmt, self.address_string(), *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(200, _index_html(feed), "text/html; charset=utf-8")
            if path == "/stats":
                body = json.dumps({**feed.rig.stats(), "viewers": feed.viewers()}, default=str).encode()
                return self._send(200, body, "application/json")
            if path.startswith("/snapshot/") and path.endswith(".jpg"):
                name = path[len("/snapshot/"):-4]
                if name not in feed.streams:
                    return self._send(404, b"unknown camera", "text/plain")
                jpg = feed.snapshot(name)
                return self._send(200, jpg, "image/jpeg") if jpg else self._send(503, b"no frame yet", "text/plain")
            if path.startswith("/stream/") and path.endswith(".mjpg"):
                name = path[len("/stream/"):-5]
                if name not in feed.streams:
                    return self._send(404, b"unknown camera", "text/plain")
                self.send_response(200)
                self.send_header("Content-Type", MJPEG_CONTENT_TYPE)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    for part in feed.iter_multipart(name):
                        self.wfile.write(part)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return None
            return self._send(404, b"not found", "text/plain")

    return Handler


def serve_mjpeg(feed: MjpegFeed, host: str = "127.0.0.1", port: int = 8090) -> ThreadingHTTPServer:
    """Start the MJPEG HTTP server on a daemon thread; call ``.shutdown()`` to stop it."""
    srv = ThreadingHTTPServer((host, port), make_handler(feed))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="cam-mjpeg-http", daemon=True).start()
    return srv
