"""Thin SpacetimeDB client interface for the orchestrator (TDM-18).

The orchestrator's bus (``bus.py``) only talks to the small surface below:

    client = SpacetimeClient(host=..., db=..., token=...)
    client.subscribe(["SELECT * FROM event_log", ...])
    client.on_snapshot(cb)   # cb(tables: {table: [Row]})   - every (re)connect, full subscribed state
    client.on_insert(cb)     # cb(table, row, meta)
    client.on_update(cb)     # cb(table, old_row, new_row, meta)   - delete+insert of one pk in one txn
    client.on_delete(cb)     # cb(table, row, meta)
    client.on_state(cb)      # cb(state, detail)  state in connecting|subscribing|live|reconnecting|stopped
    await client.run()       # reconnects forever, replaying the subscription
    await client.call_reducer("append_event", [...])
    client.stop()

The wire protocol lives in the TDM-7 spike client (``spikes/tdm-7-spacetime/stdb_ws.py``) until
TDM-16 promotes it to a shared package. Swapping is the single import line marked SWAP below.

Rows are normalised using the published schema so both wire shapes (JSON objects in the initial
subscription, positional arrays in transaction updates) look the same to handlers:
  * timestamp columns  -> int microseconds since the epoch
  * identity columns   -> "0x..." hex string
  * option columns     -> the value, or None
Rows are ``Row`` dicts that also allow attribute access (``row.status``).
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

# services/orchestrator/src/rdf_orchestrator/stdb.py -> repo root is parents[4]
_SPIKE_DIR = Path(__file__).resolve().parents[4] / "spikes" / "tdm-7-spacetime"
if str(_SPIKE_DIR) not in sys.path:
    sys.path.insert(0, str(_SPIKE_DIR))

# SWAP: when TDM-16 lands, import these from the shared package instead (same names).
from stdb_ws import StdbSubscriber, call_reducer_http, sql, ts_micros  # noqa: E402

__all__ = ["Row", "SpacetimeClient", "call_reducer_http", "sql", "ts_micros", "load_schema"]

STATES = ("connecting", "subscribing", "live", "reconnecting", "stopped")


class Row(dict):
    """A table row: a dict keyed by snake_case column name, with attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


# --------------------------------------------------------------------------- schema + normalising

TS_KEY = "__timestamp_micros_since_unix_epoch__"
ID_KEY = "__identity__"
CONN_KEY = "__connection_id__"


def _col_kind(t: dict) -> tuple:
    """Classify an algebraic type into the few shapes we normalise."""
    if "Product" in t:
        els = t["Product"]["elements"]
        if len(els) == 1:
            name = (els[0].get("name") or {}).get("some")
            if name == TS_KEY:
                return ("ts",)
            if name in (ID_KEY, CONN_KEY):
                return ("id", name)
    if "Sum" in t:
        vs = t["Sum"]["variants"]
        names = [(v.get("name") or {}).get("some") for v in vs]
        if names == ["some", "none"]:
            return ("opt", _col_kind(vs[0]["algebraic_type"]))
    return ("raw",)


def _norm(kind: tuple, v: Any) -> Any:
    k = kind[0]
    if k == "raw" or v is None:
        return v
    if k == "ts":
        return ts_micros(v)
    if k == "id":
        if isinstance(v, dict):
            v = v.get(kind[1], v)
        elif isinstance(v, list) and len(v) == 1:
            v = v[0]
        return f"0x{v:064x}" if isinstance(v, int) else v
    if k == "opt":
        if isinstance(v, dict):  # object form {"some": x} | {"none": []}
            return _norm(kind[1], v["some"]) if "some" in v else None
        if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):  # positional [tag, value]
            return _norm(kind[1], v[1]) if v[0] == 0 else None
        return v
    return v


class TableSchema:
    def __init__(self, name: str, columns: list[str], kinds: list[tuple], pk: list[str]):
        self.name, self.columns, self.kinds, self.pk = name, columns, kinds, pk

    def normalise(self, row: dict | list) -> Row:
        if isinstance(row, list):
            row = dict(zip(self.columns, row))
        out = Row(row)
        for col, kind in zip(self.columns, self.kinds):
            if kind[0] != "raw" and col in out:
                out[col] = _norm(kind, out[col])
        return out

    def key(self, row: dict) -> Any:
        if self.pk:
            vals = tuple(row.get(c) for c in self.pk)
            return vals[0] if len(vals) == 1 else vals
        return json.dumps(row, sort_keys=True, default=str)


def load_schema(host: str, db: str, timeout: float = 10.0) -> dict[str, TableSchema]:
    """GET /schema?version=9 -> {table: TableSchema} (columns in order, kinds, primary key)."""
    url = f"{host.rstrip('/')}/v1/database/{db}/schema?version=9"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        d = json.loads(r.read())
    types = d["typespace"]["types"]
    out: dict[str, TableSchema] = {}
    for t in d["tables"]:
        els = types[t["product_type_ref"]]["Product"]["elements"]
        cols = [e["name"]["some"] for e in els]
        kinds = [_col_kind(e["algebraic_type"]) for e in els]
        pk = [cols[i] for i in t.get("primary_key") or []]
        out[t["name"]] = TableSchema(t["name"], cols, kinds, pk)
    return out


# --------------------------------------------------------------------------- client

class SpacetimeClient:
    """Subscribe / on_insert / on_update / on_delete / call_reducer over one WebSocket."""

    def __init__(self, *, host: str, db: str, token: str | None, min_backoff: float = 1.0, max_backoff: float = 30.0):
        self.host, self.db, self.token = host, db, token
        self._min_backoff, self._max_backoff = min_backoff, max_backoff
        self._queries: list[str] = []
        self._cb: dict[str, Callable | None] = dict.fromkeys(("snapshot", "insert", "update", "delete", "state"))
        self.schema: dict[str, TableSchema] = {}
        self._sub: StdbSubscriber | None = None
        self._pending_deletes: dict[tuple[str, Any], tuple[Row, dict]] = {}
        self._flush_scheduled = False
        self._connects = 0
        self.state = "stopped"
        self.last_error: str | None = None

    # ---- the interface
    def subscribe(self, queries: list[str]) -> None:
        self._queries = list(queries)

    def on_snapshot(self, cb):
        self._cb["snapshot"] = cb

    def on_insert(self, cb):
        self._cb["insert"] = cb

    def on_update(self, cb):
        self._cb["update"] = cb

    def on_delete(self, cb):
        self._cb["delete"] = cb

    def on_state(self, cb):
        self._cb["state"] = cb

    def key(self, table: str, row: dict) -> Any:
        """Primary-key value of a row (tuple for composite keys; the whole row if the table has none)."""
        return self._table(table).key(row)

    @property
    def identity(self) -> str | None:
        return self._sub.identity if self._sub else None

    async def run(self) -> None:
        self._set_state("connecting")
        while not self.schema:
            try:
                self.schema = await asyncio.to_thread(load_schema, self.host, self.db)
            except Exception as e:  # noqa: BLE001 - server down at boot: keep trying
                self._raw_error(e)
                await asyncio.sleep(self._min_backoff)
        self._sub = StdbSubscriber(
            self._queries,
            on_insert=self._raw_insert,
            on_delete=self._raw_delete,
            on_applied=self._raw_applied,
            on_connect=self._raw_connect,
            on_error=self._raw_error,
            host=self.host,
            db=self.db,
            token=self.token,
            min_backoff=self._min_backoff,
            max_backoff=self._max_backoff,
        )
        try:
            await self._sub.run()
        finally:
            self._set_state("stopped")

    def stop(self) -> None:
        if self._sub:
            self._sub.stop()

    async def call_reducer(self, reducer: str, args: list | dict) -> int:
        """Fire-and-forget over the open socket; the result arrives as a transaction update."""
        if self._sub is None:
            raise RuntimeError("client not running")
        return await self._sub.call_reducer(reducer, args)

    async def force_reconnect(self) -> None:
        """Close the current socket (testing / ops): the run loop reconnects and resyncs."""
        ws = getattr(self._sub, "_ws", None)
        if ws is not None:
            await ws.close()

    # ---- spike callbacks -> normalised interface callbacks
    def _set_state(self, state: str, detail: str | None = None) -> None:
        self.state = state
        if self._cb["state"]:
            self._cb["state"](state, detail)

    def _table(self, name: str) -> TableSchema:
        ts = self.schema.get(name)
        if ts is None:
            cols = (self._sub.columns.get(name) if self._sub else None) or []
            ts = TableSchema(name, cols, [("raw",)] * len(cols), [])
        return ts

    def _raw_connect(self, identity: str, token: str) -> None:
        self._set_state("subscribing")

    def _raw_error(self, e: Exception) -> None:
        self.last_error = f"{type(e).__name__}: {e}"
        if isinstance(e, RuntimeError) and str(e).startswith("reducer failed"):
            return  # a failed reducer call is not a connection drop
        self._set_state("reconnecting", self.last_error)

    def _raw_applied(self, tables: dict[str, list]) -> None:
        if self._connects:  # reconnect: refresh the schema in the background (republished module)
            asyncio.get_running_loop().create_task(self._refresh_schema())
        self._connects += 1
        snap = {name: [self._table(name).normalise(r) for r in rows] for name, rows in tables.items()}
        self._set_state("live")
        if self._cb["snapshot"]:
            self._cb["snapshot"](snap)

    def _raw_delete(self, table: str, row: dict, meta: dict) -> None:
        ts = self._table(table)
        r = ts.normalise(row)
        self._pending_deletes[(table, ts.key(r))] = (r, meta)
        self._schedule_flush()

    def _raw_insert(self, table: str, row: dict, meta: dict) -> None:
        ts = self._table(table)
        r = ts.normalise(row)
        old = self._pending_deletes.pop((table, ts.key(r)), None) if ts.pk else None
        if old is not None:
            if self._cb["update"]:
                self._cb["update"](table, old[0], r, meta)
        elif self._cb["insert"]:
            self._cb["insert"](table, r, meta)

    async def _refresh_schema(self) -> None:
        try:
            self.schema = await asyncio.to_thread(load_schema, self.host, self.db)
        except Exception as e:  # noqa: BLE001 - keep the old schema
            self.last_error = f"schema reload failed: {e}"

    def _schedule_flush(self) -> None:
        # The spike delivers one transaction's deletes then inserts synchronously inside a single
        # message dispatch on the event loop, so call_soon runs after the whole transaction:
        # deletes still unpaired by then are real deletes.
        if not self._flush_scheduled:
            self._flush_scheduled = True
            asyncio.get_running_loop().call_soon(self._flush_deletes)

    def _flush_deletes(self) -> None:
        self._flush_scheduled = False
        pending, self._pending_deletes = self._pending_deletes, {}
        if self._cb["delete"]:
            for (table, _k), (row, meta) in pending.items():
                self._cb["delete"](table, row, meta)
