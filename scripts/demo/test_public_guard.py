"""make demo-public edge (public_guard.py) against fake upstreams. No network, no cloudflared.

    cd services/orchestrator && uv run --quiet --with 'pytest>=8' pytest -q ../../scripts/demo/test_public_guard.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_guard as g  # noqa: E402

CODE = "K7QM2X"
SECRET = "s3cret-path-0123456789abcdef"
CF = {"cf-connecting-ip": "203.0.113.9", "host": "abc.trycloudflare.com"}
HTML = {"accept": "text/html,application/xhtml+xml"}


def test_access_cookie_roundtrip():
    v = g.mint_access(CODE, now=1000)
    assert g.verify_access(CODE, v, now=1001)
    assert g.verify_access("k7qm-2x", v, now=1001), "case and dashes do not matter"
    assert not g.verify_access("OTHER1", v, now=1001)
    assert not g.verify_access(CODE, v, now=1000 + g.ACCESS_TTL_S + 1), "expired"
    body, exp, sig = v.split(".")
    assert not g.verify_access(CODE, f"{body}.{int(exp) + 5}.{sig}", now=1001), "tampered expiry"
    assert not g.verify_access("", v, now=1001) and not g.verify_access(CODE, None)
    assert g.code_matches(CODE, " k7qm2x ") and not g.code_matches(CODE, 123) and not g.code_matches("", "")


def test_limiter_window():
    lim = g.Limiter(rules=[g.Rule("x", lambda m, p, w: p == "/a", 3, 60)])
    assert all(lim.check("ip", "GET", "/a", False, now=0) is None for _ in range(3))
    hit = lim.check("ip", "GET", "/a", False, now=1)
    assert hit and hit[0] == "x" and hit[1] > 50
    assert lim.check("other", "GET", "/a", False, now=1) is None, "per client IP"
    assert lim.check("ip", "GET", "/a", False, now=61) is None, "new window"


def test_stdb_allow_list():
    ok = [("GET", "/v1/ping", False), ("POST", "/v1/identity", False), ("POST", "/v1/identity/websocket-token", False),
          ("GET", "/v1/identity/" + "a" * 64 + "/verify", False), ("GET", "/v1/database/rdfdemo/subscribe", True)]
    bad = [("POST", "/v1/database/rdfdemo/sql", False), ("POST", "/v1/database/rdfdemo/call/claim_task", False),
           ("GET", "/v1/database/rdfdemo/subscribe", False), ("GET", "/v1/database/other/subscribe", True),
           ("POST", "/v1/database/rdfdemo", False), ("DELETE", "/v1/database/rdfdemo", False),
           ("GET", "/v1/database/rdfdemo/logs", False), ("GET", "/v1/database/rdfdemo/schema", False)]
    assert all(g.stdb_allowed(m, p, w, "rdfdemo") for m, p, w in ok)
    assert not any(g.stdb_allowed(m, p, w, "rdfdemo") for m, p, w in bad)


def _echo_app(tag: str) -> web.Application:
    async def ws(req: web.Request) -> web.WebSocketResponse:
        w = web.WebSocketResponse(protocols=("v3.bsatn.spacetimedb", "v2.bsatn.spacetimedb"))
        await w.prepare(req)
        async for m in w:
            if m.type == WSMsgType.BINARY:
                await w.send_bytes(b"echo:" + m.data)
            elif m.type == WSMsgType.TEXT:
                if m.data == "close4001":
                    await w.close(code=4001, message=b"expired")
                    break
                await w.send_str("echo:" + m.data)
        return w

    async def any_(req: web.Request) -> web.Response:
        if req.headers.get("upgrade", "").lower() == "websocket":
            return await ws(req)
        if req.path == "/api/ops/session" and req.headers.get("x-tdm111-wrong-passcode"):
            return web.json_response({"code": "bad_passcode"}, status=401)
        return web.json_response({"tag": tag, "path": req.path, "qs": req.query_string, "method": req.method,
                                  "access": req.headers.get("x-rdf-access"), "host": req.headers.get("host"),
                                  "proto": req.headers.get("x-forwarded-proto")})

    app = web.Application()
    app.router.add_route("*", "/{t:.*}", any_)
    return app


@pytest.fixture()
def run():
    """run(coro_fn) with a client for a guard in front of fake web / stdb / stream / api upstreams."""
    def _run(fn):
        async def main():
            ups = {k: TestServer(_echo_app(k)) for k in ("web", "stdb", "stream", "api")}
            for s in ups.values():
                await s.start_server()
            url = {k: f"http://127.0.0.1:{s.port}" for k, s in ups.items()}
            cfg = g.Config(web=url["web"], stdb=url["stdb"], db="rdfdemo", stream=url["stream"], api=url["api"],
                           api_secret=SECRET, access_code=CODE, public_url="https://abc.trycloudflare.com")
            client = TestClient(TestServer(g.make_app(cfg)))
            await client.start_server()
            try:
                await fn(client)
            finally:
                await client.close()
                for s in ups.values():
                    await s.close()
        asyncio.run(main())
    return _run


def test_local_requests_pass_ungated(run):
    async def t(c: TestClient):
        r = await c.get("/marketplace", headers=HTML)
        assert r.status == 200 and (await r.json())["access"] == "local"
        r = await c.get("/_rdf/access")
        b = await r.json()
        assert b["code"] == CODE and b["public_url"] == "https://abc.trycloudflare.com", "the laptop sees the code"
    run(t)


def test_tunnel_needs_the_access_code(run):
    async def t(c: TestClient):
        r = await c.get("/marketplace?task=3", headers={**CF, **HTML}, allow_redirects=False)
        assert r.status == 302 and r.headers["location"] == "/join?next=/marketplace%3Ftask%3D3"
        r = await c.get("/marketplace?task=3&src=discord", headers={**CF, **HTML}, allow_redirects=False)
        from urllib.parse import parse_qs, urlsplit
        assert parse_qs(urlsplit(r.headers["location"]).query)["next"] == ["/marketplace?task=3&src=discord"], \
            "next keeps every query param (was cut at the first &)"
        r = await c.get("/api/tdm64/payments/mode", headers=CF)
        assert r.status == 401 and (await r.json())["code"] == "access_code_required"
        r = await c.get("/join", headers={**CF, **HTML, "x-rdf-access": "ok"})  # a spoofed header is dropped
        b = await r.json()
        assert r.status == 200 and b["access"] == "required" and b["host"] == "abc.trycloudflare.com" and b["proto"] == "https"
        assert (await c.get("/_next/static/chunks/a.js", headers=CF)).status == 200
        b = await (await c.get("/_rdf/access", headers=CF)).json()
        assert b["required"] and not b["ok"] and "code" not in b, "never the code through the tunnel"
        r = await c.get("/v1/ping", headers=CF)
        assert r.status == 401, "SpacetimeDB too"

        assert (await c.post("/_rdf/access", json={"code": "WRONG1"}, headers=CF)).status == 401
        r = await c.post("/_rdf/access", json={"code": "k7qm2x"}, headers=CF)
        assert r.status == 200
        sc = r.headers["set-cookie"]
        assert "HttpOnly" in sc and "Secure" in sc and "SameSite=Lax" in sc
        cookie = {"cookie": sc.split(";")[0]}
        r = await c.get("/marketplace", headers={**CF, **HTML, **cookie})
        assert (await r.json())["access"] == "ok"
        r = await c.get("/v1/ping", headers={**CF, **cookie})
        assert (await r.json())["tag"] == "stdb"
    run(t)


def test_stdb_and_stream_websockets(run):
    async def t(c: TestClient):
        v = g.mint_access(CODE)
        hdr = {**CF, "cookie": f"{g.ACCESS_COOKIE}={v}"}
        ws = await c.ws_connect("/v1/database/rdfdemo/subscribe?compression=None", headers=hdr,
                                protocols=("v3.bsatn.spacetimedb", "v2.bsatn.spacetimedb"))
        assert ws.protocol == "v3.bsatn.spacetimedb"
        await ws.send_bytes(b"\x00hi")
        assert (await ws.receive()).data == b"echo:\x00hi"
        await ws.close()
        r = await c.get("/v1/database/rdfdemo/subscribe", headers={**CF, "upgrade": "websocket", "connection": "upgrade"})
        assert r.status == 401, "no cookie, no socket"
        assert (await c.post("/v1/database/rdfdemo/sql", data="SELECT * FROM task", headers=hdr)).status == 403

        # the robot stream: prefix stripped, token-protected upstream, so no cookie needed; close codes pass through
        b = await (await c.get("/_cam/healthz", headers=CF)).json()
        assert b["tag"] == "stream" and b["path"] == "/healthz" and b["access"] is None
        ws = await c.ws_connect("/_cam/ws/cameras", headers=CF)
        await ws.send_str("hello")
        assert (await ws.receive()).data == "echo:hello"
        await ws.send_str("close4001")
        msg = await ws.receive()
        assert msg.type == WSMsgType.CLOSE and ws.close_code == 4001
    run(t)


def test_ws_message_rate_limit(run):
    async def t(c: TestClient):
        hdr = {**CF, "cookie": f"{g.ACCESS_COOKIE}={g.mint_access(CODE)}"}
        ws = await c.ws_connect("/v1/database/rdfdemo/subscribe", headers=hdr, protocols=("v2.bsatn.spacetimedb",))
        for i in range(150):  # burst 100
            if ws.closed:
                break
            await ws.send_bytes(b"x")
        while not ws.closed:
            m = await ws.receive()
            if m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
                break
        assert ws.close_code == 1008
    run(t)


def test_api_capability_path(run):
    async def t(c: TestClient):
        assert (await c.get("/_api/wrong-secret-0123456789/healthz", headers=CF)).status == 404
        b = await (await c.get(f"/_api/{SECRET}/healthz", headers=CF)).json()
        assert b["tag"] == "api" and b["path"] == "/healthz"
        assert (await c.post(f"/_api/{SECRET}/uploads/sign-batch", json={}, headers=CF)).status == 200
        for m, p in (("POST", "/tickets"), ("POST", "/tickets/1/advance"), ("POST", "/stream/token"), ("GET", "/media/list")):
            assert (await c.request(m, f"/_api/{SECRET}{p}", headers=CF)).status == 403, p
    run(t)


def test_rate_limit_on_access_code(run):
    async def t(c: TestClient):
        codes = [(await c.post("/_rdf/access", json={"code": "NOPE00"}, headers=CF)).status for _ in range(31)]
        assert codes[:30] == [401] * 30 and codes[30] == 429
        r = await c.post("/_rdf/access", json={"code": CODE}, headers=CF)
        assert r.status == 429 and int(r.headers["retry-after"]) > 0, "even the right code waits out the window"
        assert (await c.post("/_rdf/access", json={"code": CODE}, headers={**CF, "cf-connecting-ip": "198.51.100.7"})).status == 200
    run(t)


def test_tdm111_public_pages_and_operator_login(run):
    """TDM-111: landing, operator login and short links open without the code; the rest stays gated."""
    assert g.public_open("/") and g.public_open("/ops/login") and g.public_open("/j/12/discord")
    assert not g.public_open("/marketplace") and not g.public_open("/tickets/1") and not g.public_open("/j/12")
    assert not g.public_open("/ops/settings") and not g.public_open("/api/tdm74/tickets")

    async def t(c: TestClient):
        for p in ("/", "/join", "/ops/login", "/j/12/discord"):
            r = await c.get(p, headers={**CF, **HTML}, allow_redirects=False)
            assert r.status == 200 and (await r.json())["access"] == "required", p
        for p in ("/marketplace", "/tickets/1", "/live", "/ops/settings"):
            r = await c.get(p, headers={**CF, **HTML}, allow_redirects=False)
            assert r.status == 302 and r.headers["location"].startswith("/join?next="), p
        assert (await c.get("/api/tdm64/payments/mode", headers=CF)).status == 401

        r = await c.post("/api/ops/session", json={"passcode": "x"}, headers={**CF, "x-tdm111-wrong-passcode": "1"})
        assert r.status == 401 and "set-cookie" not in r.headers, "a wrong passcode gets no access cookie"
        r = await c.post("/api/ops/session", json={"passcode": "x"}, headers=CF)
        assert r.status == 200
        sc = [v for v in r.headers.getall("set-cookie") if v.startswith(g.ACCESS_COOKIE + "=")]
        assert sc and "HttpOnly" in sc[0] and "Secure" in sc[0]
        r = await c.get("/marketplace", headers={**CF, **HTML, "cookie": sc[0].split(";")[0]}, allow_redirects=False)
        assert r.status == 200 and (await r.json())["access"] == "ok", "the operator now has access"
    run(t)
