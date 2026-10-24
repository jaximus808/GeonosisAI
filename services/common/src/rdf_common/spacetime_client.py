"""SpacetimeDB client for the Python services (TDM-16; protocol choice from TDM-7 / S-02).

There is no maintained Python SDK for SpacetimeDB 2.x, so this speaks the server's own
interfaces directly: HTTP for reducer calls and SQL, and the JSON WebSocket protocol
(``v1.json.spacetimedb``) for live subscriptions. Dependencies: ``httpx`` and ``websockets``.
It does not import pydantic or the settings loader, so a Modal job or the Jetson can use it
with only those two packages installed.

Reducer calls (keep-alive ``httpx`` client; ~120 ms per call instead of 1-3 s of TLS)::

    from rdf_common.spacetime_client import SpacetimeClient
    with SpacetimeClient() as stdb:                      # SPACETIME_HOST/DB_NAME/TOKEN from env
        stdb.call_reducer("append_event", "planner", "plan.decision", '{"choice":"recovery"}')
        stdb.call_reducer("append_event", actor="planner", type="x", payload_json="{}")
        rows = stdb.sql("SELECT * FROM ticket WHERE status = 'baseline'")   # list[dict]

``AsyncSpacetimeClient`` has the same API with ``await``.

Subscriptions (reconnect with 1 s -> 30 s backoff, then re-subscribe)::

    from rdf_common.spacetime_client import subscribe
    sub = subscribe(
        ["SELECT * FROM robot_command WHERE robot_id = 1"],
        on_insert=lambda table, row, meta: ...,
        on_update=lambda table, old, new, meta: ...,
        on_delete=lambda table, row, meta: ...,
    )                       # runs on a daemon thread (sync code: robot client, Modal jobs)
    sub.wait_ready(10)
    sub.rows("robot_command")   # live cache of the subscribed rows
    sub.stop()

In asyncio code (orchestrator) build ``Subscription(...)`` and ``await sub.run()`` instead.

Rows are dicts keyed by the server-side snake_case column names, decoded with ``/schema``:
``Option`` -> value or ``None``, ``Timestamp`` -> int microseconds since the Unix epoch,
``Identity`` -> ``"0x..."`` hex string. Reducer arguments use the same Python values and are
encoded the same way (``None`` -> none, int -> Timestamp, hex string -> Identity).
Typed dataclasses for every table live in ``rdf_common.spacetime_tables`` (generated; pass
``typed=True`` to a subscription to get them instead of dicts).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import httpx
import websockets

PROTOCOL = "v1.json.spacetimedb"
DEFAULT_HOST = "https://maincloud.spacetimedb.com"

Row = dict[str, Any]
Meta = dict[str, Any]  # reducer, request_id, caller, timestamp (int micros) of the transaction
InsertCb = Callable[[str, Any, Meta], None]  # (table, row, meta)
UpdateCb = Callable[[str, Any, Any, Meta], None]  # (table, old_row, new_row, meta)
DeleteCb = Callable[[str, Any, Meta], None]  # (table, row, meta)

_TS = "__timestamp_micros_since_unix_epoch__"
_ID = "__identity__"
_CONN = "__connection_id__"
_DUR = "__time_duration_micros__"


class SpacetimeError(RuntimeError):
    """A reducer call or SQL query was rejected. ``status`` is the HTTP status (530 = reducer threw)."""

    def __init__(self, status: int, message: str, *, reducer: str | None = None):
        self.status, self.message, self.reducer = status, message, reducer
        where = f"reducer {reducer!r}" if reducer else "request"
        super().__init__(f"{where} failed ({status}): {message}")


# --------------------------------------------------------------------------- config


def load_dotenv(path: str | os.PathLike | None = None) -> None:
    """Fill missing env vars from the repo ``.env`` (scripts; services use rdf_common.settings)."""
    from pathlib import Path

    env = Path(path) if path else Path(__file__).resolve().parents[4] / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))



@dataclass(frozen=True)
class SpacetimeConfig:
    host: str
    db: str
    token: str | None

    @classmethod
    def resolve(cls, host: str | None = None, db: str | None = None, token: str | None = None) -> "SpacetimeConfig":
        host = (host or os.environ.get("SPACETIME_HOST") or DEFAULT_HOST).rstrip("/")
        db = db or os.environ.get("SPACETIME_DB_NAME")
        if not db:
            raise RuntimeError("SPACETIME_DB_NAME is not set (load .env or pass db=)")
        # token=None -> env; token="" -> deliberately anonymous
        token = os.environ.get("SPACETIME_TOKEN") if token is None else (token or None)
        return cls(host, db, token)

    @classmethod
    def from_settings(cls, settings: Any) -> "SpacetimeConfig":
        """From an ``rdf_common.settings.SpacetimeSettings`` (or anything with those attributes)."""
        tok = settings.spacetime_token
        tok = tok.get_secret_value() if hasattr(tok, "get_secret_value") else tok
        return cls.resolve(settings.spacetime_host, settings.spacetime_db_name, tok or "")

    @property
    def base(self) -> str:
        return f"{self.host}/v1/database/{self.db}"

    @property
    def ws_url(self) -> str:
        return self.base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/subscribe"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


# --------------------------------------------------------------------------- schema + value codec


def ts_micros(v: Any) -> int:
    """Raw or decoded Timestamp -> int microseconds since the epoch."""
    if isinstance(v, dict):
        return int(v[_TS])
    if isinstance(v, list):
        return int(v[0])
    if isinstance(v, _dt.datetime):
        return int(v.timestamp() * 1_000_000)
    return int(v)


def ts_to_datetime(micros: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(micros / 1_000_000, tz=_dt.timezone.utc)


def now_micros() -> int:
    return int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1_000_000)


def _special(ty: dict) -> str | None:
    """Name of the single field of a SATS special product (Timestamp, Identity, ...), else None."""
    els = ty.get("Product", {}).get("elements") if isinstance(ty.get("Product"), dict) else None
    if els and len(els) == 1:
        n = (els[0].get("name") or {}).get("some")
        if n in (_TS, _ID, _CONN, _DUR):
            return n
    return None


def _option_inner(ty: dict) -> dict | None:
    """Inner type if ``ty`` is Option<T> (variants some/none), else None."""
    s = ty.get("Sum")
    if not s:
        return None
    vs = s["variants"]
    if len(vs) == 2 and [(v.get("name") or {}).get("some") for v in vs] == ["some", "none"]:
        return vs[0]["algebraic_type"]
    return None


def _name(n: Any) -> str:
    return n["some"] if isinstance(n, dict) else n


class Schema:
    """The parts of ``GET /schema?version=9`` the client needs: columns, types, primary keys, reducers."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.typespace: list[dict] = raw["typespace"]["types"]
        self.tables: dict[str, dict] = {}
        for t in raw["tables"]:
            els = self.resolve({"Ref": t["product_type_ref"]})["Product"]["elements"]
            self.tables[t["name"]] = {
                "columns": [(_name(e["name"]), e["algebraic_type"]) for e in els],
                "primary_key": [_name(els[i]["name"]) for i in t.get("primary_key", [])],
                "public": "Public" in (t.get("table_access") or {}),
            }
        self.reducers: dict[str, list[tuple[str, dict]]] = {
            r["name"]: [(_name(e["name"]), e["algebraic_type"]) for e in r["params"]["elements"]]
            for r in raw["reducers"]
        }

    def resolve(self, ty: dict) -> dict:
        while "Ref" in ty:
            ty = self.typespace[ty["Ref"]]
        return ty

    def columns(self, table: str) -> list[str]:
        return [c for c, _ in self.tables[table]["columns"]]

    def primary_key(self, table: str) -> list[str]:
        return self.tables[table]["primary_key"]

    # ---- decode: wire JSON (object or positional form) -> plain Python
    def decode(self, ty: dict, v: Any) -> Any:
        ty = self.resolve(ty)
        sp = _special(ty)
        if sp is not None:
            inner = v.get(sp) if isinstance(v, dict) else (v[0] if isinstance(v, list) else v)
            return inner if sp in (_ID, _CONN) else int(inner)
        if "Product" in ty:
            els = ty["Product"]["elements"]
            if isinstance(v, list):
                return {_name(e["name"]): self.decode(e["algebraic_type"], x) for e, x in zip(els, v)}
            return {_name(e["name"]): self.decode(e["algebraic_type"], v.get(_name(e["name"]))) for e in els}
        if "Sum" in ty:
            tag, val = self._sum_parts(ty, v)
            inner = _option_inner(ty)
            if inner is not None:
                return self.decode(inner, val) if tag == "some" else None
            variant = next(x for x in ty["Sum"]["variants"] if _name(x["name"]) == tag)
            vty = self.resolve(variant["algebraic_type"])
            if vty.get("Product", {}).get("elements") == []:
                return tag  # unit variant (plain enum) -> its name
            return {tag: self.decode(vty, val)}
        if "Array" in ty:
            if isinstance(v, str):  # Vec<u8> is sent as hex
                return bytes.fromhex(v)
            return [self.decode(ty["Array"], x) for x in v]
        return v

    def _sum_parts(self, ty: dict, v: Any) -> tuple[str, Any]:
        names = [_name(x["name"]) for x in ty["Sum"]["variants"]]
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
            return names[v[0]], v[1]
        if isinstance(v, dict) and len(v) == 1:
            (k, val), = v.items()
            return (names[int(k)] if k.isdigit() else k), val
        raise ValueError(f"cannot decode sum value {v!r}")

    def decode_row(self, table: str, raw: Any) -> Row:
        if isinstance(raw, str):
            raw = json.loads(raw)
        cols = self.tables[table]["columns"]
        if isinstance(raw, list):
            return {c: self.decode(t, x) for (c, t), x in zip(cols, raw)}
        return {c: self.decode(t, raw.get(c)) for c, t in cols}

    # ---- encode: plain Python -> wire JSON (reducer args)
    def encode(self, ty: dict, v: Any) -> Any:
        ty = self.resolve(ty)
        sp = _special(ty)
        if sp is not None:
            if isinstance(v, dict):
                return v
            if sp == _TS:
                return {_TS: ts_micros(v)}
            if sp == _ID and isinstance(v, str) and not v.startswith("0x"):
                v = "0x" + v
            return {sp: v}
        inner = _option_inner(ty)
        if inner is not None:
            if isinstance(v, dict) and set(v) <= {"some", "none"} and len(v) == 1:
                return v  # already wire form
            return {"none": []} if v is None else {"some": self.encode(inner, v)}
        if "Product" in ty:
            if isinstance(v, dict):
                return {_name(e["name"]): self.encode(e["algebraic_type"], v.get(_name(e["name"])))
                        for e in ty["Product"]["elements"]}
            return v
        if "Sum" in ty:
            if isinstance(v, str):  # unit variant by name
                return {v: []}
            return v
        if "Array" in ty:
            if isinstance(v, (bytes, bytearray)):
                return bytes(v).hex()
            return [self.encode(ty["Array"], x) for x in v]
        return v

    def encode_args(self, reducer: str, args: tuple, kwargs: dict) -> list:
        params = self.reducers.get(reducer)
        if params is None:
            raise SpacetimeError(404, f"no such reducer in the published schema: {reducer}", reducer=reducer)
        if args and kwargs:
            raise TypeError("pass reducer args positionally or by name, not both")
        if kwargs:
            unknown = set(kwargs) - {p for p, _ in params}
            if unknown:
                raise TypeError(f"{reducer}: unknown args {sorted(unknown)}")
            missing = [p for p, t in params if p not in kwargs and _option_inner(self.resolve(t)) is None]
            if missing:
                raise TypeError(f"{reducer}: missing args {missing}")
            args = tuple(kwargs.get(p) for p, _ in params)
        if len(args) != len(params):
            raise TypeError(f"{reducer} takes {len(params)} args ({', '.join(p for p, _ in params)}), got {len(args)}")
        return [self.encode(t, a) for (_, t), a in zip(params, args)]


def _sql_result(schema_elements: list[dict], rows: list, schema: Schema | None) -> list[Row]:
    cols = [(_name(e["name"]), e["algebraic_type"]) for e in schema_elements]
    if schema is None:
        return [dict(zip([c for c, _ in cols], r)) for r in rows]
    return [{c: schema.decode(t, x) for (c, t), x in zip(cols, r)} for r in rows]


# --------------------------------------------------------------------------- HTTP clients


class _Base:
    def __init__(self, host: str | None = None, db: str | None = None, token: str | None = None,
                 *, config: SpacetimeConfig | None = None, timeout: float = 10.0):
        self.config = config or SpacetimeConfig.resolve(host, db, token)
        self.timeout = timeout
        self._schema: Schema | None = None

    def _check(self, r: httpx.Response, reducer: str | None = None) -> None:
        if r.status_code >= 300:
            raise SpacetimeError(r.status_code, r.text.strip() or r.reason_phrase, reducer=reducer)

    def _transport_kw(self) -> dict:
        return {"headers": self.config.headers(), "timeout": self.timeout}


class SpacetimeClient(_Base):
    """Sync HTTP client on one keep-alive connection. Thread-safe (httpx.Client is)."""

    def __init__(self, host: str | None = None, db: str | None = None, token: str | None = None, **kw):
        super().__init__(host, db, token, **kw)
        self._http = httpx.Client(transport=httpx.HTTPTransport(retries=2), **self._transport_kw())

    @classmethod
    def from_settings(cls, settings: Any, **kw) -> "SpacetimeClient":
        return cls(config=SpacetimeConfig.from_settings(settings), **kw)

    def schema(self, refresh: bool = False) -> Schema:
        if self._schema is None or refresh:
            r = self._http.get(f"{self.config.base}/schema", params={"version": 9})
            self._check(r)
            self._schema = Schema(r.json())
        return self._schema

    def call_reducer(self, name: str, /, *args: Any, **kwargs: Any) -> None:
        """Call a reducer; Python values are encoded from the reducer's signature.

        Raises SpacetimeError on a non-2xx answer (400 bad args, 530 the reducer threw).
        Reducers return nothing: read the effect from a subscription or ``sql()``.
        """
        body = self.schema().encode_args(name, args, kwargs)
        r = self._http.post(f"{self.config.base}/call/{name}", json=body)
        if r.status_code == 400 and self._schema is not None:
            self.schema(refresh=True)  # maybe republished; caller can retry
        self._check(r, reducer=name)

    def sql(self, query: str, *, decode: bool = True) -> list[Row]:
        """Run SQL; returns the last statement's rows as dicts (decoded like subscription rows)."""
        r = self._http.post(f"{self.config.base}/sql", content=query.encode())
        self._check(r)
        res = r.json()[-1]
        return _sql_result(res["schema"]["elements"], res["rows"], self.schema() if decode else None)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SpacetimeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncSpacetimeClient(_Base):
    """asyncio twin of SpacetimeClient (one keep-alive httpx.AsyncClient)."""

    def __init__(self, host: str | None = None, db: str | None = None, token: str | None = None, **kw):
        super().__init__(host, db, token, **kw)
        self._http = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=2), **self._transport_kw())

    @classmethod
    def from_settings(cls, settings: Any, **kw) -> "AsyncSpacetimeClient":
        return cls(config=SpacetimeConfig.from_settings(settings), **kw)

    async def schema(self, refresh: bool = False) -> Schema:
        if self._schema is None or refresh:
            r = await self._http.get(f"{self.config.base}/schema", params={"version": 9})
            self._check(r)
            self._schema = Schema(r.json())
        return self._schema

    async def call_reducer(self, name: str, /, *args: Any, **kwargs: Any) -> None:
        body = (await self.schema()).encode_args(name, args, kwargs)
        r = await self._http.post(f"{self.config.base}/call/{name}", json=body)
        if r.status_code == 400:
            await self.schema(refresh=True)
        self._check(r, reducer=name)

    async def sql(self, query: str, *, decode: bool = True) -> list[Row]:
        r = await self._http.post(f"{self.config.base}/sql", content=query.encode())
        self._check(r)
        res = r.json()[-1]
        return _sql_result(res["schema"]["elements"], res["rows"], (await self.schema()) if decode else None)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncSpacetimeClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


def fetch_schema(host: str | None = None, db: str | None = None, *, timeout: float = 10.0) -> Schema:
    cfg = SpacetimeConfig.resolve(host, db, "")
    r = httpx.get(f"{cfg.base}/schema", params={"version": 9}, timeout=timeout)
    r.raise_for_status()
    return Schema(r.json())


def create_identity(host: str | None = None, *, timeout: float = 10.0) -> tuple[str, str]:
    """``POST /v1/identity`` -> (identity_hex, token): a fresh per-service identity.

    Store the token as that service's secret; the owner then allow-lists the identity with
    ``register_service_identity``. Never log the token.
    """
    host = (host or os.environ.get("SPACETIME_HOST") or DEFAULT_HOST).rstrip("/")
    r = httpx.post(f"{host}/v1/identity", timeout=timeout)
    r.raise_for_status()
    d = r.json()
    return d["identity"], d["token"]


# --------------------------------------------------------------------------- WebSocket subscription


class Subscription:
    """Live subscription over the JSON WebSocket protocol, with reconnect and re-subscribe.

    Callbacks run on the subscription's event loop (its own thread with ``start_thread()``):
      * ``on_applied(snapshot: dict[table, list[row]])`` after every (re)subscribe. After a
        reconnect, changes made while offline show up here, not as insert/update/delete events,
        so reconcile from the snapshot (or read ``rows()``).
      * ``on_insert(table, row, meta)``, ``on_delete(table, row, meta)``
      * ``on_update(table, old, new, meta)``: a delete + insert with the same primary key in one
        transaction. Without ``on_update`` those arrive as ``on_delete`` + ``on_insert``.
      * ``on_connect(identity_hex, token)``: persist the token to keep the identity when starting
        anonymous. ``on_error(exc)``: drops, reducer failures, subscription errors (never fatal).
    ``meta`` = {"reducer", "request_id", "caller", "timestamp"} of the transaction.
    """

    def __init__(
        self,
        queries: Iterable[str],
        *,
        on_insert: InsertCb | None = None,
        on_update: UpdateCb | None = None,
        on_delete: DeleteCb | None = None,
        on_applied: Callable[[dict[str, list[Any]]], None] | None = None,
        on_connect: Callable[[str, str], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        typed: bool = False,
        decode: bool = True,
        host: str | None = None,
        db: str | None = None,
        token: str | None = None,
        config: SpacetimeConfig | None = None,
        min_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ):
        self.queries = list(queries)
        self.on_insert, self.on_update, self.on_delete = on_insert, on_update, on_delete
        self.on_applied, self.on_connect, self.on_error = on_applied, on_connect, on_error
        self.typed = typed
        self.decode = decode  # False: raw wire values (spike semantics), only positional rows mapped to dicts
        self.config = config or SpacetimeConfig.resolve(host, db, token)
        self._token = self.config.token
        self.min_backoff, self.max_backoff = min_backoff, max_backoff
        self.identity: str | None = None
        self.schema: Schema | None = None
        self.reconnects = 0
        self._cache: dict[str, dict[Any, Any]] = {}
        self._cache_lock = threading.Lock()
        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._connected: asyncio.Event | None = None
        self._req_id = 0
        self._thread: threading.Thread | None = None
        self._row_types: dict[str, Any] | None = None

    # ---------------------------------------------------------------- lifecycle
    async def run(self) -> None:
        """Connect, subscribe and dispatch; reconnect until ``stop()``."""
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        if self._connected is None:
            self._connected = asyncio.Event()
        backoff = self.min_backoff
        first = True
        while not self._stop.is_set():
            try:
                # reload the schema on every connect so a republished module is picked up
                self.schema = await asyncio.to_thread(fetch_schema, self.config.host, self.config.db)
                headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
                async with websockets.connect(
                    self.config.ws_url, subprotocols=[PROTOCOL], additional_headers=headers,
                    max_size=None, ping_interval=20, ping_timeout=20, open_timeout=15,
                ) as ws:
                    self._ws = ws
                    if not first:
                        self.reconnects += 1
                    first = False
                    await self._send({"Subscribe": {"query_strings": self.queries, "request_id": self._next_id()}})
                    async for raw in ws:
                        self._dispatch(json.loads(raw))
                        backoff = self.min_backoff  # healthy traffic resets the backoff
                        if self._stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any drop -> reconnect
                self._err(e)
            finally:
                self._ws = None
                self._connected.clear()
                self._ready.clear()
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self.max_backoff)

    def start_thread(self) -> "Subscription":
        """Run on a daemon thread (sync callers). Callbacks fire on that thread."""
        self._thread = threading.Thread(target=lambda: asyncio.run(self.run()), name="stdb-subscription", daemon=True)
        self._thread.start()
        return self

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Block until the subscription is applied (thread-safe). Returns False on timeout."""
        return self._ready.wait(timeout)

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def stop(self, join_timeout: float = 5.0) -> None:
        def _do() -> None:
            if self._stop is not None:
                self._stop.set()
            if self._ws is not None:
                asyncio.ensure_future(self._ws.close())

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(_do)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(join_timeout)

    async def force_reconnect(self) -> None:
        """Drop the socket (tests / stuck-connection recovery); run() reconnects."""
        if self._ws is not None:
            await self._ws.close()

    # ---------------------------------------------------------------- cache
    def rows(self, table: str) -> list[Any]:
        """Current subscribed rows of ``table`` (thread-safe snapshot)."""
        with self._cache_lock:
            return list(self._cache.get(table, {}).values())

    def get(self, table: str, *pk: Any) -> Any | None:
        with self._cache_lock:
            return self._cache.get(table, {}).get(pk[0] if len(pk) == 1 else tuple(pk))

    # ---------------------------------------------------------------- reducers over the socket
    async def call_reducer(self, name: str, /, *args: Any, **kwargs: Any) -> int:
        """Fire-and-forget reducer call over the open socket; returns the request id.

        The result arrives as a transaction event (``meta['request_id']``); failures go to on_error.
        """
        assert self._connected is not None, "subscription is not running"
        await self._connected.wait()
        assert self.schema is not None
        body = self.schema.encode_args(name, args, kwargs)
        rid = self._next_id()
        await self._send({"CallReducer": {"reducer": name, "args": json.dumps(body), "request_id": rid, "flags": 0}})
        return rid

    def call_reducer_threadsafe(self, name: str, /, *args: Any, **kwargs: Any) -> int:
        """Sync wrapper for ``call_reducer`` from another thread (e.g. the robot control loop)."""
        if self._loop is None:
            raise RuntimeError("subscription is not running")
        fut = asyncio.run_coroutine_threadsafe(self.call_reducer(name, *args, **kwargs), self._loop)
        return fut.result(10.0)

    # ---------------------------------------------------------------- internals
    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _send(self, msg: dict) -> None:
        await self._ws.send(json.dumps(msg))

    def _err(self, e: Exception) -> None:
        if self.on_error:
            try:
                self.on_error(e)
            except Exception:  # noqa: BLE001
                pass

    def _wrap(self, table: str, row: Row) -> Any:
        if not self.typed:
            return row
        if self._row_types is None:
            from rdf_common import spacetime_tables  # generated; imported lazily

            self._row_types = spacetime_tables.ROW_TYPES
        cls = self._row_types.get(table)
        return cls.from_row(row) if cls else row

    def _pk(self, table: str, row: Row) -> Any:
        pk = self.schema.primary_key(table) if self.schema and table in self.schema.tables else []
        if not pk:
            return json.dumps(row, sort_keys=True, default=str)  # no PK: whole row is the key
        key = tuple(row[c] for c in pk)
        if not self.decode:  # raw values may be dicts/lists (identity pk): make them hashable
            key = tuple(json.dumps(k, sort_keys=True) if isinstance(k, (dict, list)) else k for k in key)
        return key[0] if len(key) == 1 else key

    def _rows(self, t: dict, key: str) -> list[Row]:
        name = t["table_name"]
        raw = [r for u in t.get("updates", []) for r in u.get(key, [])]
        if self.decode:
            return [self.schema.decode_row(name, r) for r in raw]
        out = []
        for r in raw:
            r = json.loads(r) if isinstance(r, str) else r
            out.append(dict(zip(self.schema.columns(name), r)) if isinstance(r, list) else r)
        return out

    def _call(self, cb: Callable | None, *a: Any) -> None:
        if cb is None:
            return
        try:
            cb(*a)
        except Exception as e:  # noqa: BLE001 - a bad callback must not kill the socket
            self._err(e)

    def _dispatch(self, msg: dict) -> None:
        (kind, body), = msg.items()
        if kind == "IdentityToken":
            self.identity = body["identity"][_ID]
            self._token = body["token"]  # reuse on reconnect -> same identity even if we started anonymous
            self._call(self.on_connect, self.identity, self._token)
        elif kind == "InitialSubscription":
            snapshot: dict[str, list[Any]] = {}
            with self._cache_lock:
                self._cache = {}
                for t in body["database_update"]["tables"]:
                    rows = self._rows(t, "inserts")
                    tbl = self._cache.setdefault(t["table_name"], {})
                    for r in rows:
                        tbl[self._pk(t["table_name"], r)] = self._wrap(t["table_name"], r)
                snapshot = {name: list(tbl.values()) for name, tbl in self._cache.items()}
            self._connected.set()
            self._ready.set()
            self._call(self.on_applied, snapshot)
        elif kind in ("TransactionUpdate", "TransactionUpdateLight"):
            if kind == "TransactionUpdateLight":
                committed, call = body.get("update"), {}
            else:
                status = body.get("status") or {}
                if "Committed" not in status:
                    failed = status.get("Failed") or status.get("OutOfEnergy") or status
                    rc = body.get("reducer_call") or {}
                    self._err(SpacetimeError(530, str(failed), reducer=rc.get("reducer_name")))
                    return
                committed, call = status["Committed"], body.get("reducer_call") or {}
            ts = body.get("timestamp")
            meta: Meta = {
                "reducer": call.get("reducer_name"),
                "request_id": call.get("request_id", body.get("request_id")),
                "caller": (body.get("caller_identity") or {}).get(_ID),
                "timestamp": ts_micros(ts) if ts else None,
            }
            for t in (committed or {}).get("tables", []):
                self._apply(t, meta)
        elif kind == "SubscriptionError":
            self._err(RuntimeError(f"subscription error: {body.get('error')}"))

    def _apply(self, t: dict, meta: Meta) -> None:
        name = t["table_name"]
        deletes = {self._pk(name, r): r for r in self._rows(t, "deletes")}
        inserts = [(self._pk(name, r), r) for r in self._rows(t, "inserts")]
        events: list[tuple] = []
        with self._cache_lock:
            tbl = self._cache.setdefault(name, {})
            for k, new in inserts:
                if k in deletes and self.on_update is not None:
                    old = self._wrap(name, deletes.pop(k))
                    tbl[k] = w = self._wrap(name, new)
                    events.append((self.on_update, name, old, w, meta))
                elif k in deletes:
                    old = self._wrap(name, deletes.pop(k))
                    tbl[k] = w = self._wrap(name, new)
                    events.append((self.on_delete, name, old, meta))
                    events.append((self.on_insert, name, w, meta))
                else:
                    tbl[k] = w = self._wrap(name, new)
                    events.append((self.on_insert, name, w, meta))
            for k, r in deletes.items():
                tbl.pop(k, None)
                events.insert(0, (self.on_delete, name, self._wrap(name, r), meta))
        for cb, *a in events:
            self._call(cb, *a)


def subscribe(
    queries: Iterable[str],
    on_insert: InsertCb | None = None,
    on_update: UpdateCb | None = None,
    on_delete: DeleteCb | None = None,
    **kw: Any,
) -> Subscription:
    """Start a ``Subscription`` on a daemon thread and return it (sync convenience)."""
    return Subscription(queries, on_insert=on_insert, on_update=on_update, on_delete=on_delete, **kw).start_thread()


__all__ = [
    "AsyncSpacetimeClient",
    "Schema",
    "SpacetimeClient",
    "SpacetimeConfig",
    "SpacetimeError",
    "Subscription",
    "create_identity",
    "fetch_schema",
    "load_dotenv",
    "now_micros",
    "subscribe",
    "ts_micros",
    "ts_to_datetime",
]
