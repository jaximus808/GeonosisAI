"""Minimal SpacetimeDB 2.x client for Python services (TDM-7 / S-02).

There is no maintained Python SDK for SpacetimeDB 2.x (PyPI `spacetimedb-sdk` is 0.7 from 2023 and
speaks a dead protocol), so this talks the server's JSON WebSocket protocol (`v1.json.spacetimedb`)
directly. One dependency: `websockets`.

  * Subscribe:   StdbSubscriber(queries=[...], on_insert=..., on_delete=...).run()  (asyncio)
                 or .start_thread() for sync code (robot client on the Jetson, Modal jobs).
                 Reconnects with 1 s -> 30 s exponential backoff and replays the subscription,
                 like the TS SDK's withAutomaticReconnect().
  * Call reducer: call_reducer_http("append_event", [...])  (stdlib only, no socket needed)
                 or await sub.call_reducer(...) over the open socket.
  * Query:       sql("SELECT ...") -> list of dict rows (stdlib only; also the polling fallback).

Rows arrive as dicts keyed by the server-side (snake_case) column names. A `timestamp` column is
{"__timestamp_micros_since_unix_epoch__": int}; use ts_micros(row["ts"]).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.request
from typing import Any, Callable, Iterable

import websockets

PROTOCOL = "v1.json.spacetimedb"
Row = dict[str, Any]
RowCb = Callable[[str, Row, dict], None]  # (table_name, row, meta) ; meta has reducer/caller/timestamp


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is not set (load .env)")
    return v


def _base(host: str | None, db: str | None) -> str:
    host = (host or _env("SPACETIME_HOST", "https://maincloud.spacetimedb.com")).rstrip("/")
    return f"{host}/v1/database/{db or _env('SPACETIME_DB_NAME')}"


def _auth(token: str | None) -> dict[str, str]:
    token = token if token is not None else os.environ.get("SPACETIME_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def ts_micros(v: Any) -> int:
    """Timestamp column -> microseconds since epoch (WS gives a dict, HTTP SQL gives a 1-elem list)."""
    if isinstance(v, dict):
        return int(v["__timestamp_micros_since_unix_epoch__"])
    if isinstance(v, list):
        return int(v[0])
    return int(v)


# --------------------------------------------------------------------------- HTTP (stdlib only)

def call_reducer_http(reducer: str, args: list | dict, *, host=None, db=None, token=None, timeout=10.0) -> None:
    """POST /call/<reducer>. Raises urllib.error.HTTPError (with the server's message) on failure."""
    req = urllib.request.Request(
        f"{_base(host, db)}/call/{reducer}",
        data=json.dumps(args).encode(),
        headers={**_auth(token), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def sql(query: str, *, host=None, db=None, token=None, timeout=10.0) -> list[Row]:
    """POST /sql. Returns rows of the last statement as dicts keyed by column name."""
    req = urllib.request.Request(
        f"{_base(host, db)}/sql", data=query.encode(), headers=_auth(token), method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read())[-1]
    cols = [e["name"]["some"] if isinstance(e["name"], dict) else e["name"] for e in res["schema"]["elements"]]
    return [dict(zip(cols, row)) for row in res["rows"]]


def table_columns(*, host=None, db=None, timeout=10.0) -> dict[str, list[str]]:
    """GET /schema -> {table_name: [column names in declaration order]}.

    Needed because the server sends rows of *other* clients' transactions (TransactionUpdateLight)
    as positional JSON arrays, while initial-subscription rows come as objects.
    """
    with urllib.request.urlopen(f"{_base(host, db)}/schema?version=9", timeout=timeout) as r:
        d = json.loads(r.read())
    types = d["typespace"]["types"]
    return {
        t["name"]: [e["name"]["some"] for e in types[t["product_type_ref"]]["Product"]["elements"]]
        for t in d["tables"]
    }


# --------------------------------------------------------------------------- WebSocket subscriber

class StdbSubscriber:
    def __init__(
        self,
        queries: Iterable[str],
        *,
        on_insert: RowCb | None = None,
        on_delete: RowCb | None = None,
        on_applied: Callable[[dict[str, list[Row]]], None] | None = None,  # initial rows, per (re)connect
        on_connect: Callable[[str, str], None] | None = None,  # (identity_hex, token) - persist token to keep identity
        on_error: Callable[[Exception], None] | None = None,
        host: str | None = None,
        db: str | None = None,
        token: str | None = None,
        min_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ):
        self.queries = list(queries)
        self.on_insert, self.on_delete, self.on_applied = on_insert, on_delete, on_applied
        self.on_connect, self.on_error = on_connect, on_error
        self.url = _base(host, db).replace("https://", "wss://").replace("http://", "ws://") + "/subscribe"
        self.token = token if token is not None else os.environ.get("SPACETIME_TOKEN")
        self.min_backoff, self.max_backoff = min_backoff, max_backoff
        self.identity: str | None = None
        self._ws = None
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._req_id = 0
        self._host, self._db = host, db
        self.columns: dict[str, list[str]] = {}
        self.connected = asyncio.Event()

    # ---- lifecycle
    async def run(self) -> None:
        """Connect, subscribe, dispatch; reconnect forever until stop()."""
        self._loop = asyncio.get_running_loop()
        backoff = self.min_backoff
        while not self._stop.is_set():
            try:
                # (re)load column order each connect so a republished schema is picked up
                self.columns = await asyncio.to_thread(table_columns, host=self._host, db=self._db)
                async with websockets.connect(
                    self.url, subprotocols=[PROTOCOL], additional_headers=_auth(self.token),
                    max_size=None, ping_interval=20, ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    await self._send({"Subscribe": {"query_strings": self.queries, "request_id": self._next_id()}})
                    async for raw in ws:
                        self._dispatch(json.loads(raw))
                        backoff = self.min_backoff  # healthy traffic resets the backoff
                        if self._stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any drop -> reconnect
                if self.on_error:
                    self.on_error(e)
            finally:
                self._ws = None
                self.connected.clear()
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self.max_backoff)

    def stop(self) -> None:
        def _do():
            self._stop.set()
            if self._ws is not None:
                asyncio.ensure_future(self._ws.close())
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(_do)
        else:
            self._stop.set()

    def start_thread(self) -> threading.Thread:
        """Run the subscriber on a daemon thread (for sync code). Callbacks fire on that thread."""
        t = threading.Thread(target=lambda: asyncio.run(self.run()), name="stdb-subscriber", daemon=True)
        t.start()
        return t

    # ---- reducer over the open socket (fire-and-forget; result arrives as a TransactionUpdate)
    async def call_reducer(self, reducer: str, args: list | dict) -> int:
        await self.connected.wait()
        rid = self._next_id()
        await self._send({"CallReducer": {"reducer": reducer, "args": json.dumps(args), "request_id": rid, "flags": 0}})
        return rid

    # ---- internals
    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _send(self, msg: dict) -> None:
        await self._ws.send(json.dumps(msg))

    def _rows(self, update: dict, key: str) -> list[Row]:
        cols = self.columns.get(update["table_name"])
        out = []
        for u in update.get("updates", []):
            for r in u.get(key, []):
                r = json.loads(r) if isinstance(r, str) else r
                if isinstance(r, list):  # positional row (TransactionUpdateLight) -> dict
                    if cols is None:
                        raise RuntimeError(f"no schema for table {update['table_name']}")
                    r = dict(zip(cols, r))
                out.append(r)
        return out

    def _dispatch(self, msg: dict) -> None:
        (kind, body), = msg.items()
        if kind == "IdentityToken":
            self.identity = body["identity"]["__identity__"]
            self.token = body["token"]  # reuse on reconnect -> same identity even if we started anonymous
            if self.on_connect:
                self.on_connect(self.identity, self.token)
        elif kind == "InitialSubscription":
            self.connected.set()
            if self.on_applied:
                tables = body["database_update"]["tables"]
                self.on_applied({t["table_name"]: self._rows(t, "inserts") for t in tables})
        elif kind in ("TransactionUpdate", "TransactionUpdateLight"):
            status = body.get("status", {"Committed": body.get("update")})
            committed = status.get("Committed") if isinstance(status, dict) else None
            if committed is None:
                if self.on_error and isinstance(status, dict) and "Failed" in status:
                    self.on_error(RuntimeError(f"reducer failed: {status['Failed']}"))
                return
            meta = {
                "reducer": (body.get("reducer_call") or {}).get("reducer_name"),
                "request_id": (body.get("reducer_call") or {}).get("request_id"),
                "caller": (body.get("caller_identity") or {}).get("__identity__"),
                "timestamp": body.get("timestamp"),
            }
            for t in committed.get("tables", []):
                name = t["table_name"]
                if self.on_delete:
                    for row in self._rows(t, "deletes"):
                        self.on_delete(name, row, meta)
                if self.on_insert:
                    for row in self._rows(t, "inserts"):
                        self.on_insert(name, row, meta)
        elif kind == "SubscriptionError":
            if self.on_error:
                self.on_error(RuntimeError(f"subscription error: {body.get('error')}"))
