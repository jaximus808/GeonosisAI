"""TDM-110 hybrid backend edge (public_guard.py without --web) and hybrid.py's stream-URL helpers.
No network, no cloudflared.

    cd services/orchestrator && uv run --quiet --with 'pytest>=8' pytest -q ../../scripts/demo/test_tdm110_hybrid.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import public_guard as g  # noqa: E402

EDGE = "e" * 32
PUBLIC = "https://api-xyz.trycloudflare.com"
CF = {"cf-connecting-ip": "203.0.113.9", "host": "api-xyz.trycloudflare.com"}


def _tdm110_echo(tag: str) -> web.Application:
    async def any_(req: web.Request) -> web.Response:
        return web.json_response({"tag": tag, "path": req.path, "method": req.method,
                                  "secret": req.headers.get(g.EDGE_SECRET_HEADER)})
    app = web.Application()
    app.router.add_route("*", "/{t:.*}", any_)
    return app


@pytest.fixture()
def tdm110_run():
    def _run(fn, *, stdb: bool = True):
        async def main():
            ups = {k: TestServer(_tdm110_echo(k)) for k in ("orch", "stdb")}
            for s in ups.values():
                await s.start_server()
            url = {k: f"http://127.0.0.1:{s.port}" for k, s in ups.items()}
            cfg = g.Config(web=None, stdb=url["stdb"] if stdb else None, db="rdfdemo", orch=url["orch"],
                           edge_secret=EDGE, public_url=PUBLIC)
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


def test_proof_binds_nonce_and_url(tdm110_run):
    async def t(c: TestClient):
        n = "n" * 20
        b = await (await c.get(f"/_rdf/proof?nonce={n}", headers=CF)).json()
        assert b == {"v": 1, "url": PUBLIC, "proof": g.edge_proof(EDGE, n, PUBLIC)}
        assert b["proof"] != g.edge_proof(EDGE, n, "https://other.trycloudflare.com")
        assert b["proof"] != g.edge_proof(EDGE, "m" * 20, PUBLIC)
        assert (await c.get("/_rdf/proof?nonce=short", headers=CF)).status == 400
    tdm110_run(t)


def test_orchestrator_needs_the_edge_secret(tdm110_run):
    async def t(c: TestClient):
        r = await c.post("/tickets", json={}, headers=CF)
        assert r.status == 401 and (await r.json())["code"] == "edge_secret_required"
        assert (await c.get("/healthz", headers={**CF, g.EDGE_SECRET_HEADER: "e" * 31})).status == 401
        r = await c.post("/tickets", json={}, headers={**CF, g.EDGE_SECRET_HEADER: EDGE})
        b = await r.json()
        assert r.status == 200 and b["tag"] == "orch" and b["path"] == "/tickets"
        assert b["secret"] is None, "the secret is stripped before the orchestrator"
        assert (await c.get("/marketplace", headers=CF)).status == 401, "no web app on this edge"
    tdm110_run(t)


def test_stdb_server_routes_only_with_the_secret(tdm110_run):
    async def t(c: TestClient):
        assert (await c.get("/v1/ping", headers=CF)).status == 200, "browser allow-list, no access code here"
        assert (await c.post("/v1/database/rdfdemo/sql", data="SELECT 1", headers=CF)).status == 403
        r = await c.post("/v1/database/rdfdemo/sql", data="SELECT 1", headers={**CF, g.EDGE_SECRET_HEADER: EDGE})
        assert r.status == 200 and (await r.json())["tag"] == "stdb"
        r = await c.post("/v1/database/rdfdemo/call/approve_spend", json=[], headers={**CF, g.EDGE_SECRET_HEADER: EDGE})
        assert r.status == 200
        r = await c.post("/v1/database/other/sql", data="x", headers={**CF, g.EDGE_SECRET_HEADER: EDGE})
        assert r.status == 403, "only this edge's db"
        r = await c.delete("/v1/database/rdfdemo", headers={**CF, g.EDGE_SECRET_HEADER: EDGE})
        assert r.status == 403, "never publish/delete"
    tdm110_run(t)


def test_no_stdb_without_expose(tdm110_run):
    async def t(c: TestClient):
        assert (await c.get("/v1/ping", headers=CF)).status == 404
    tdm110_run(t, stdb=False)


def test_rebase_stream_moves_every_url_to_the_robot_tunnel():
    import importlib

    sys.modules.setdefault("public", importlib.import_module("public"))
    import hybrid as h

    old = {"transport": "jpeg-ws", "url": "ws://127.0.0.1:8791/ws/cameras", "http": "http://127.0.0.1:8791",
           "control_url": "ws://127.0.0.1:8791/ws/control", "state_url": "ws://127.0.0.1:8791/ws/state", "cams": []}
    new = h.rebase_stream(old, "https://robot-abc.trycloudflare.com/")
    assert new["url"] == "wss://robot-abc.trycloudflare.com/ws/cameras"
    assert new["http"] == "https://robot-abc.trycloudflare.com"
    assert new["control_url"] == "wss://robot-abc.trycloudflare.com/ws/control"
    assert new["state_url"] == "wss://robot-abc.trycloudflare.com/ws/state"
    assert new["transport"] == "jpeg-ws" and new["tunnel"] == "hybrid"
    assert h.stream_on(new, "https://robot-abc.trycloudflare.com") and not h.stream_on(old, "https://robot-abc.trycloudflare.com")
    pub = {"url": "wss://app.example.com/_cam/ws/cameras", "http": "https://app.example.com/_cam"}
    assert h.rebase_stream(pub, "https://r.example.com")["url"] == "wss://r.example.com/ws/cameras"
