"""`make demo-public` edge: the ONE local origin a Cloudflare tunnel points at.

    python scripts/demo/public_guard.py --listen 127.0.0.1:3747 --web http://127.0.0.1:3748 \
        --stdb http://127.0.0.1:3030 --db rdfdemo [--stream http://127.0.0.1:8791] \
        [--api http://127.0.0.1:8030]      # env: RDF_PUBLIC_ACCESS_CODE, RDF_PUBLIC_API_SECRET, RDF_PUBLIC_URL

Why one origin: the phone only ever talks to ``https://<app>``, so there is one tunnel (one quick-tunnel
URL, or one ``app.<domain>`` hostname), no CORS, and the web build does not depend on the tunnel URL.

Routes (by path):
  /_rdf/healthz             200 {"ok": true}                           (no secrets)
  /_rdf/access              GET {required, ok, public_url, code*}      POST {code} -> sets the access cookie
                            (* the code itself only to requests from this laptop, never through the tunnel)
  /v1/...                   SpacetimeDB, ALLOW-LIST only: ping, identity create / websocket-token / verify,
                            and the subscribe WebSocket of --db. No /sql, no HTTP reducer calls, no
                            publish / delete / logs. Needs the access cookie. Reducers still check ctx.sender
                            (the owner token never leaves this laptop).
  /_cam/...                 the robot's TDM-55 stream server (prefix stripped): /ws/cameras, /ws/control,
                            /ws/state, /snapshot. Every one already needs a short-lived HMAC stream token, so
                            no access cookie here (the orchestrator's reset-photo fetch has none).
  /_api/<secret>/...        the orchestrator, only for a REMOTE Jetson / Modal (--api): healthz, uploads/sign,
                            uploads/sign-batch, modal/progress, modal/callback. The secret path is the
                            capability (JETSON_ORCHESTRATOR_URL=https://<app>/_api/<secret>). Off by default.
  everything else           the Next.js app (``next start``). Needs the access cookie, except /join (which shows
                            the code form), /_next/static and the icons. Without the cookie: pages redirect
                            to /join?next=..., API and WebSocket calls get 401.

"Through the tunnel" = the request carries Cloudflare's ``cf-connecting-ip`` (or a non-loopback Host).
Only those are gated and rate limited, per client IP; the laptop itself (http://localhost:<port>) is not.
The guard listens on 127.0.0.1 only. Upstream, Next gets ``x-rdf-access: local|ok|required`` (any client
value is dropped), ``x-forwarded-proto: https`` and the public Host.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, TCPConnector, WSMsgType, web
from aiohttp.client_exceptions import ClientError

log = logging.getLogger("rdf.public_guard")

ACCESS_COOKIE = "rdf_access"
ACCESS_TTL_S = 7 * 24 * 3600
ACCESS_LABEL = b"rdf-public-access-v1"
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
WS_HANDSHAKE = {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol",
                "sec-websocket-accept"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
STATIC_OPEN = re.compile(r"^/(_next/static/|favicon\.ico$|icon|apple-icon|manifest\.webmanifest$)")
API_ALLOW = {("GET", "/healthz"), ("POST", "/uploads/sign"), ("POST", "/uploads/sign-batch"),
             ("POST", "/modal/progress"), ("POST", "/modal/callback")}


# ----------------------------------------------------------------------------------------- access code
def normalize_code(code: str) -> str:
    return re.sub(r"[\s\-_.]", "", code or "").upper()


def _access_key(code: str) -> bytes:
    return hmac.new(normalize_code(code).encode(), ACCESS_LABEL, hashlib.sha256).digest()


def mint_access(code: str, now: float | None = None, ttl_s: int = ACCESS_TTL_S) -> str:
    exp = int((now if now is not None else time.time()) + ttl_s)
    body = f"v1.{exp}"
    sig = base64.urlsafe_b64encode(hmac.new(_access_key(code), body.encode(), hashlib.sha256).digest()).rstrip(b"=")
    return f"{body}.{sig.decode()}"


def verify_access(code: str, value: str | None, now: float | None = None) -> bool:
    if not code or not value:
        return False
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != "v1" or not parts[1].isdigit():
        return False
    now = now if now is not None else time.time()
    exp = int(parts[1])
    if exp <= now or exp > now + ACCESS_TTL_S + 60:
        return False
    want = mint_access(code, now=exp, ttl_s=0)  # same body -> same sig
    return hmac.compare_digest(want, value)


def code_matches(code: str, candidate: object) -> bool:
    if not code or not isinstance(candidate, str):
        return False
    a = hashlib.sha256(normalize_code(candidate).encode()).digest()
    b = hashlib.sha256(normalize_code(code).encode()).digest()
    return hmac.compare_digest(a, b)


# ----------------------------------------------------------------------------------------- rate limits
@dataclass
class Rule:
    name: str
    match: Callable[[str, str, bool], bool]   # (method, path, is_ws)
    limit: int
    window_s: float


def default_rules() -> list[Rule]:
    return [
        # Per client IP. A venue's Wi-Fi often NATs every phone to ONE IP, so these are sized for a room
        # of judges on one address, not for one phone: they stop scripts, not people.
        Rule("access_code", lambda m, p, w: m == "POST" and p == "/_rdf/access", 30, 600),
        Rule("ops_login", lambda m, p, w: m == "POST" and p == "/api/ops/session", 30, 600),
        Rule("stream_token", lambda m, p, w: m == "POST" and p in ("/api/tdm55/stream-token",
                                                                   "/api/robotview/state-token"), 300, 60),
        Rule("join_page", lambda m, p, w: m == "GET" and p == "/join", 300, 60),
        Rule("stdb_identity", lambda m, p, w: m == "POST" and p == "/v1/identity", 60, 60),
        Rule("stdb_connect", lambda m, p, w: w and p.startswith("/v1/"), 120, 60),
        Rule("cam_connect", lambda m, p, w: w and p.startswith("/_cam/"), 300, 60),
        Rule("api", lambda m, p, w: p.startswith("/api/"), 1500, 60),
        Rule("any", lambda m, p, w: True, 6000, 60),
    ]


@dataclass
class Limiter:
    rules: list[Rule] = field(default_factory=default_rules)
    _hits: dict[tuple[str, str], tuple[float, int]] = field(default_factory=dict)

    def check(self, ip: str, method: str, path: str, is_ws: bool, now: float | None = None) -> tuple[str, float] | None:
        """None when allowed, else (rule name, retry-after seconds). Counts the request in every matching rule."""
        now = now if now is not None else time.monotonic()
        refused = None
        for r in self.rules:
            if not r.match(method, path, is_ws):
                continue
            k = (r.name, ip)
            start, n = self._hits.get(k, (now, 0))
            if now - start >= r.window_s:
                start, n = now, 0
            n += 1
            self._hits[k] = (start, n)
            if n > r.limit and refused is None:
                refused = (r.name, max(1.0, r.window_s - (now - start)))
        if len(self._hits) > 50_000:  # prune old windows
            self._hits = {k: v for k, v in self._hits.items() if now - v[0] < 600}
        return refused


@dataclass
class Bucket:
    """Per-connection token bucket for client->server SpacetimeDB WebSocket messages (reducer calls,
    subscriptions). The browser sends a few per second at most (claim heartbeats, presence)."""
    rate: float = 20.0
    burst: float = 100.0
    tokens: float = 100.0
    at: float = field(default_factory=time.monotonic)

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.at) * self.rate)
        self.at = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


# ----------------------------------------------------------------------------------------- the guard
@dataclass
class Config:
    web: str
    stdb: str
    db: str
    stream: str | None = None
    api: str | None = None
    api_secret: str = ""
    access_code: str = ""
    public_url: str = ""


def stdb_allowed(method: str, path: str, is_ws: bool, db: str) -> bool:
    if path == "/v1/ping":
        return method in ("GET", "HEAD")
    if path in ("/v1/identity", "/v1/identity/websocket-token"):
        return method == "POST"
    if re.fullmatch(r"/v1/identity/(0x)?[0-9a-fA-F]{64}/verify", path):
        return method == "GET"
    if path == f"/v1/database/{db}/subscribe":
        return is_ws
    return False


class Guard:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.limiter = Limiter()
        self.session: ClientSession | None = None
        self.stats = {"requests": 0, "tunneled": 0, "refused_access": 0, "rate_limited": 0, "blocked": 0}

    # -- helpers
    @staticmethod
    def tunneled(req: web.Request) -> bool:
        if req.headers.get("cf-connecting-ip"):
            return True
        host = (req.headers.get("host") or "").lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        return bool(name) and name not in LOOPBACK_HOSTS

    @staticmethod
    def client_ip(req: web.Request) -> str:
        return (req.headers.get("cf-connecting-ip") or (req.headers.get("x-forwarded-for") or "").split(",")[0].strip()
                or (req.remote or "local"))

    def has_access(self, req: web.Request) -> bool:
        return verify_access(self.cfg.access_code, req.cookies.get(ACCESS_COOKIE))

    @staticmethod
    def is_ws(req: web.Request) -> bool:
        return req.headers.get("upgrade", "").lower() == "websocket"

    @staticmethod
    def jerr(status: int, code: str, detail: str, **headers: str) -> web.Response:
        return web.json_response({"code": code, "detail": detail}, status=status,
                                 headers={"cache-control": "no-store", **headers})

    # -- entry
    async def handle(self, req: web.Request) -> web.StreamResponse:
        self.stats["requests"] += 1
        path, method, ws = req.path, req.method, self.is_ws(req)
        tunneled = self.tunneled(req)
        if tunneled:
            self.stats["tunneled"] += 1
            hit = self.limiter.check(self.client_ip(req), method, path, ws)
            if hit:
                self.stats["rate_limited"] += 1
                log.warning("rate limited %s %s (%s)", method, path, hit[0])
                return self.jerr(429, "rate_limited", f"Too many requests ({hit[0]}). Try again shortly.",
                                 **{"retry-after": str(int(hit[1]))})
        if path == "/_rdf/healthz":
            return web.json_response({"ok": True}, headers={"cache-control": "no-store"})
        if path == "/_rdf/access":
            return await self.access(req, tunneled)

        gate = bool(self.cfg.access_code) and tunneled
        if path.startswith("/_api/"):
            return await self.api(req)
        if path == "/_cam" or path.startswith("/_cam/"):
            if not self.cfg.stream:
                return self.jerr(404, "no_stream", "No robot stream is served here.")
            return await self.proxy(req, self.cfg.stream, path[len("/_cam"):] or "/", tunneled, access="n/a")
        if path.startswith("/v1/"):
            if not stdb_allowed(method, path, ws, self.cfg.db):
                self.stats["blocked"] += 1
                return self.jerr(403, "not_allowed", "This SpacetimeDB route is not exposed publicly.")
            if gate and not self.has_access(req):
                self.stats["refused_access"] += 1
                return self.jerr(401, "access_code_required", "Enter the access code at /join first.")
            return await self.proxy(req, self.cfg.stdb, path, tunneled, access="n/a", ws_limit=tunneled)

        if not gate:
            access = "local" if not tunneled else "off"
        elif self.has_access(req):
            access = "ok"
        else:
            access = "required"
            if not (path == "/join" or STATIC_OPEN.match(path)):
                self.stats["refused_access"] += 1
                if method in ("GET", "HEAD") and not ws and not path.startswith("/api/") \
                        and "text/html" in req.headers.get("accept", ""):
                    nxt = quote(req.path_qs, safe="/?=&")
                    raise web.HTTPFound(f"/join?next={nxt}")
                return self.jerr(401, "access_code_required", "Enter the access code at /join first.")
        return await self.proxy(req, self.cfg.web, path, tunneled, access=access)

    # -- /_rdf/access
    async def access(self, req: web.Request, tunneled: bool) -> web.Response:
        required = bool(self.cfg.access_code) and tunneled
        if req.method == "GET":
            body: dict = {"required": bool(self.cfg.access_code), "ok": (not required) or self.has_access(req),
                          "public_url": self.cfg.public_url or None}
            if self.cfg.access_code and not tunneled:
                body["code"] = self.cfg.access_code  # the presenter's laptop only (shown next to the QR)
            return web.json_response(body, headers={"cache-control": "no-store"})
        if req.method != "POST":
            return self.jerr(405, "method_not_allowed", "GET or POST")
        if not self.cfg.access_code:
            return web.json_response({"ok": True, "required": False}, headers={"cache-control": "no-store"})
        try:
            b = await req.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            b = {}
        cand = b.get("code") if isinstance(b, dict) else None
        if not code_matches(self.cfg.access_code, cand):
            log.info("wrong access code from %s", self.client_ip(req) if tunneled else "local")
            return self.jerr(401, "bad_code", "That code is not right. It is on the screen next to the QR code.")
        resp = web.json_response({"ok": True}, headers={"cache-control": "no-store"})
        resp.headers["set-cookie"] = "; ".join([
            f"{ACCESS_COOKIE}={mint_access(self.cfg.access_code)}", "Path=/", f"Max-Age={ACCESS_TTL_S}", "HttpOnly",
            "SameSite=Lax", *(["Secure"] if tunneled else [])])
        return resp

    # -- /_api/<secret>/...
    async def api(self, req: web.Request) -> web.StreamResponse:
        m = re.fullmatch(r"/_api/([A-Za-z0-9_-]{16,})(/.*)", req.path)
        if not (self.cfg.api and self.cfg.api_secret and m and hmac.compare_digest(m.group(1), self.cfg.api_secret)):
            self.stats["blocked"] += 1
            return self.jerr(404, "not_found", "Not found.")
        sub = m.group(2)
        if (req.method, sub) not in API_ALLOW:
            self.stats["blocked"] += 1
            return self.jerr(403, "not_allowed", "This orchestrator route is not exposed publicly.")
        return await self.proxy(req, self.cfg.api, sub, self.tunneled(req), access="n/a")

    # -- proxying
    def upstream_headers(self, req: web.Request, tunneled: bool, access: str) -> dict[str, str]:
        h = {k: v for k, v in req.headers.items() if k.lower() not in HOP and k.lower() not in WS_HANDSHAKE
             and k.lower() != "x-rdf-access"}
        if access != "n/a":
            h["x-rdf-access"] = access
        if req.headers.get("host"):
            h["x-forwarded-host"] = req.headers["host"]
        if tunneled:
            h["x-forwarded-proto"] = "https"
        return h

    async def proxy(self, req: web.Request, base: str, path: str, tunneled: bool, *, access: str,
                    ws_limit: bool = False) -> web.StreamResponse:
        assert self.session is not None
        qs = req.query_string
        url = base.rstrip("/") + path + (f"?{qs}" if qs else "")
        headers = self.upstream_headers(req, tunneled, access)
        if self.is_ws(req):
            return await self.proxy_ws(req, url, headers, ws_limit)
        host = req.headers.get("host")
        try:
            body = await req.read() if req.can_read_body else None
            async with self.session.request(req.method, url, headers={**headers, **({"host": host} if host else {})},
                                            data=body, allow_redirects=False) as up:
                resp = web.StreamResponse(status=up.status, reason=up.reason)
                for k, v in up.headers.items():
                    if k.lower() not in HOP:
                        resp.headers.add(k, v)
                if up.headers.get("content-length"):  # bodies pass through still encoded (auto_decompress=False)
                    resp.content_length = int(up.headers["content-length"])
                await resp.prepare(req)
                try:
                    async for chunk in up.content.iter_any():
                        await resp.write(chunk)
                    await resp.write_eof()
                except (ConnectionResetError, ClientError, asyncio.TimeoutError) as e:
                    # the browser went away mid-body (cancelled prefetch, closed tab): nothing left to answer
                    log.debug("stream to client ended early (%s): %s", path, e)
                return resp
        except (ClientError, asyncio.TimeoutError) as e:
            log.warning("upstream %s failed: %s", base, e)
            return self.jerr(502, "upstream_unreachable", "The local service behind this link is not answering.")

    async def proxy_ws(self, req: web.Request, url: str, headers: dict[str, str], limit: bool) -> web.StreamResponse:
        assert self.session is not None
        protos = [p.strip() for p in req.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        url = re.sub(r"^http", "ws", url)
        try:
            up = await self.session.ws_connect(url, headers=headers, protocols=protos, max_msg_size=0,
                                               autoping=True, heartbeat=None)
        except (ClientError, asyncio.TimeoutError) as e:
            log.warning("upstream ws %s failed: %s", url.split("?")[0], e)
            return self.jerr(502, "upstream_unreachable", "The local service behind this link is not answering.")
        ws = web.WebSocketResponse(protocols=[up.protocol] if up.protocol else (), max_msg_size=0, heartbeat=30)
        await ws.prepare(req)
        bucket = Bucket() if limit else None

        async def client_to_up() -> None:
            async for msg in ws:
                if bucket is not None and not bucket.take():
                    self.stats["rate_limited"] += 1
                    log.warning("ws message rate limit: closing %s", req.path)
                    await ws.close(code=1008, message=b"rate limited")
                    break
                if msg.type == WSMsgType.BINARY:
                    await up.send_bytes(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await up.send_str(msg.data)
                else:
                    break

        async def up_to_client() -> None:
            async for msg in up:
                if msg.type == WSMsgType.BINARY:
                    await ws.send_bytes(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await ws.send_str(msg.data)
                else:
                    break

        tasks = [asyncio.create_task(client_to_up()), asyncio.create_task(up_to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            code = up.close_code if up.closed and up.close_code else 1000
            if not ws.closed:
                await ws.close(code=code if 1000 <= code < 5000 and code not in (1005, 1006) else 1000)
            if not up.closed:
                await up.close(code=ws.close_code if ws.close_code and 1000 <= ws.close_code < 5000
                               and ws.close_code not in (1005, 1006) else 1000)
        return ws


def make_app(cfg: Config) -> web.Application:
    guard = Guard(cfg)

    async def on_startup(app: web.Application) -> None:
        guard.session = ClientSession(connector=TCPConnector(limit=0), auto_decompress=False,
                                      timeout=ClientTimeout(total=None, sock_connect=10))

    async def on_cleanup(app: web.Application) -> None:
        if guard.session:
            await guard.session.close()

    app = web.Application(client_max_size=12 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", guard.handle)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", default="127.0.0.1:3747")
    ap.add_argument("--web", required=True)
    ap.add_argument("--stdb", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--stream", default=None)
    ap.add_argument("--api", default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host, port = a.listen.rsplit(":", 1)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("refusing to listen on a non-loopback address: the tunnel connects locally", file=sys.stderr)
        return 2
    cfg = Config(web=a.web, stdb=a.stdb, db=a.db, stream=a.stream, api=a.api,
                 api_secret=os.environ.get("RDF_PUBLIC_API_SECRET", ""),
                 access_code=os.environ.get("RDF_PUBLIC_ACCESS_CODE", ""),
                 public_url=os.environ.get("RDF_PUBLIC_URL", "").rstrip("/"))
    if cfg.api and len(cfg.api_secret) < 16:
        print("--api needs RDF_PUBLIC_API_SECRET (16+ url-safe chars)", file=sys.stderr)
        return 2
    log.info("guard on %s:%s -> web %s, stdb %s (db %s), stream %s, api %s, access code %s", host, port, a.web, a.stdb,
             a.db, a.stream or "-", "on" if cfg.api else "off", "on" if cfg.access_code else "OFF")
    web.run_app(make_app(cfg), host=host, port=int(port), print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
