"""TDM-57 [MOCK] robot /control server: the TDM-55 token + TDM-57 lease guard, no arm, no IK.

Stands in for TDM-52's real /control until it lands. It speaks the teleop contract's auth and command
messages, gates every motion message through ``rdf_robot_client.tdm57_lease.ControlLeaseGuard``
(re-reads the task row from SpacetimeDB every 2 s, fails closed after 5 s without a good read) and,
instead of moving anything, counts what it would have applied. Every reply carries ``mock: true``.

    cd services/robot-client && uv run python ../../spikes/tdm-57-lease/mock_control.py \
        --port 8957 --robot-id 1 --stdb-host http://127.0.0.1:3057 --stdb-db tdm57lease

Protocol (same socket path family as /ws/cameras):
  client -> {"type":"auth","token":...} first (and on refresh), then {"t":"cmd",seq,...,task_id,claimed_at_us}
            or {"t":"stop"}
  server -> {"t":"auth_ok",exp,task_id,claimed_at_us} | {"t":"ack",seq} | {"t":"rejected",seq,code}
            | {"t":"lease_lost",code,detail} then close 4004 | {"t":"error",code} then close 4003/4001
GET /healthz -> counters and the last 50 decisions (JSON, no auth).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import deque
from urllib.parse import urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from rdf_common.spacetime_client import SpacetimeClient
from rdf_common.stream_token import StreamTokenError, load_secret, verify
from rdf_robot_client.tdm57_lease import CLOSE_LEASE_LOST, ControlLeaseGuard, spacetime_task_fetcher

log = logging.getLogger("tdm57.mock_control")
CLOSE_EXPIRED, CLOSE_FORBIDDEN = 4001, 4003


class MockControl:
    def __init__(self, robot_id: int, stdb: SpacetimeClient, secret, recheck_s: float = 2.0):
        self.robot_id, self.secret, self.recheck_s = robot_id, secret, recheck_s
        self.fetch = spacetime_task_fetcher(stdb)
        self.stats = {"connections": 0, "denied": 0, "accepted": 0, "rejected": 0, "stops": 0, "lease_lost": 0}
        self.events: deque[dict] = deque(maxlen=50)
        self.active: dict | None = None

    def note(self, **kw) -> None:
        kw["t"] = round(time.time(), 3)
        self.events.append(kw)
        log.info("%s", kw)

    def check_token(self, token: str):
        claims = verify(self.secret, token)
        if claims.robot_id != self.robot_id:
            raise StreamTokenError("wrong_robot")
        return claims

    def http(self, conn: ServerConnection, req: Request) -> Response | None:
        u = urlsplit(req.path)
        if u.path == "/control":
            return None
        hdr = Headers({"Content-Type": "application/json", "Cache-Control": "no-store",
                       "Access-Control-Allow-Origin": "*"})
        if u.path == "/healthz":
            body = {"mock": True, "robot_id": self.robot_id, "stats": self.stats, "active": self.active,
                    "events": list(self.events)}
            return Response(200, "OK", hdr, json.dumps(body).encode())
        return Response(404, "Not Found", hdr, b"{}")

    async def send(self, ws: ServerConnection, obj: dict) -> None:
        try:
            await ws.send(json.dumps({**obj, "mock": True}))
        except ConnectionClosed:
            pass

    async def handler(self, ws: ServerConnection) -> None:
        self.stats["connections"] += 1
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), 5.0))
            if first.get("type") != "auth":
                raise StreamTokenError("auth_required")
            claims = self.check_token(str(first.get("token", "")))
        except (asyncio.TimeoutError, json.JSONDecodeError, StreamTokenError) as e:
            code = e.code if isinstance(e, StreamTokenError) else "auth_required"
            self.stats["denied"] += 1
            self.note(ev="denied", code=code)
            await self.send(ws, {"t": "error", "code": code})
            await ws.close(CLOSE_EXPIRED if code == "expired" else CLOSE_FORBIDDEN, code)
            return
        except ConnectionClosed:
            return

        admitted = False

        async def lost(d):
            if not admitted:
                return  # the handler answers a refused admit itself
            self.stats["lease_lost"] += 1
            self.note(ev="lease_lost", code=d.code, task_id=claims.task_id, sub=claims.sub)
            await self.send(ws, {"t": "lease_lost", "code": d.code, "detail": d.detail})
            await ws.close(CLOSE_LEASE_LOST, f"lease_lost:{d.code}")

        guard = ControlLeaseGuard(claims, self.fetch, robot_id=self.robot_id, recheck_s=self.recheck_s,
                                  on_lost=lost)
        d = await guard.admit_async()
        if not d.ok:
            self.stats["denied"] += 1
            self.note(ev="admit_refused", code=d.code, task_id=claims.task_id, sub=claims.sub)
            await self.send(ws, {"t": "lease_lost", "code": d.code, "detail": d.detail})
            await ws.close(CLOSE_LEASE_LOST, f"lease_lost:{d.code}")
            return
        admitted = True
        self.note(ev="admitted", task_id=claims.task_id, sub=claims.sub, claimed_at_us=claims.claimed_at_us)
        self.active = {"task_id": claims.task_id, "sub": claims.sub, "since": time.time()}
        await self.send(ws, {"t": "auth_ok", "exp": claims.exp, "task_id": claims.task_id,
                             "claimed_at_us": claims.claimed_at_us})
        watcher = asyncio.create_task(guard.watch())
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    continue
                if m.get("type") == "auth":
                    try:
                        dd = guard.update_token(self.check_token(str(m.get("token", ""))))
                    except StreamTokenError as e:
                        dd = None
                        self.note(ev="refresh_bad_token", code=e.code)
                    if dd is not None and not dd.ok:
                        self.note(ev="refresh_refused", code=dd.code)
                    continue
                if m.get("t") == "stop":
                    self.stats["stops"] += 1
                    self.note(ev="stop")
                    await self.send(ws, {"t": "ack", "seq": m.get("seq"), "stop": True})
                    continue
                if m.get("t") not in ("cmd", "takeover", "handback", "record_start", "record_stop"):
                    continue
                dd = guard.check_message(m)
                if dd.ok:
                    self.stats["accepted"] += 1
                    await self.send(ws, {"t": "ack", "seq": m.get("seq")})
                else:
                    self.stats["rejected"] += 1
                    self.note(ev="rejected", code=dd.code, seq=m.get("seq"))
                    await self.send(ws, {"t": "rejected", "seq": m.get("seq"), "code": dd.code})
        except ConnectionClosed:
            pass
        finally:
            watcher.cancel()
            self.active = None
            self.note(ev="closed", task_id=claims.task_id, sub=claims.sub, guard=guard.status())


async def main_async(a: argparse.Namespace) -> None:
    stdb = SpacetimeClient(a.stdb_host, a.stdb_db, "")  # task is a public table: anonymous read
    srv = MockControl(a.robot_id, stdb, load_secret(), recheck_s=a.recheck)
    async with serve(srv.handler, "127.0.0.1", a.port, process_request=srv.http):
        log.info("[MOCK] /control on ws://127.0.0.1:%s/control (robot %s)", a.port, a.robot_id)
        await asyncio.Future()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8957)
    ap.add_argument("--robot-id", type=int, required=True)
    ap.add_argument("--stdb-host", required=True)
    ap.add_argument("--stdb-db", required=True)
    ap.add_argument("--recheck", type=float, default=2.0)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
