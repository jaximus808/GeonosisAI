"""SpacetimeDB subscription runtime: every orchestrator behaviour is a handler on a table change.

Register handlers per table name with decorators on a ``Registry`` (the module-level ``registry``
is the one the app runs):

    from rdf_orchestrator.bus import on_insert, on_update, on_enter

    @on_enter("training_run", where=lambda r: r.status == "queued")
    def launch_training(ev):            # sync handlers run on the worker pool
        ...                             # ev.row, ev.old, ev.meta, ev.ticket_id, ev.bus

    @on_insert("event_log")
    async def tap(ev):                  # async handlers run on the event loop
        ...

Kinds:
  * ``on_insert(table, where=, include_existing=False)``  a new row (where() on the row)
  * ``on_update(table, where=)``                            a row changed (where() on the new row)
  * ``on_delete(table, where=)``                            a row went away
  * ``on_enter(table, where, include_existing=True)``       a row *starts* matching where(): inserted
    matching, or updated from not-matching to matching. This is the restart-safe one for state
    machines ("training_run became queued").

Restart / reconnect safety (the Fly machine may restart, SpacetimeDB may drop):
  * The bus keeps an in-memory mirror of every subscribed table, rebuilt from the DB on every
    (re)connect, never trusted across a process restart. ``bus.rows(table)`` / ``bus.get(table, pk)``.
  * First snapshot after boot: ``include_existing`` handlers fire for rows already in the DB
    (``ev.source == "snapshot"``), so in-flight work (a queued run, a waiting agent) is picked up.
  * Snapshot after a reconnect: the bus diffs it against the mirror and fires insert/update/delete
    for whatever changed while the socket was down (``ev.source == "resync"``).
  * Handlers must therefore be idempotent and decide from the row (and reducer-side status checks),
    not from process memory. Handlers for the same row can run concurrently on the pool; re-read
    current state with ``ev.bus.get(...)`` before acting if order matters.
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .log import get_logger, ticket_id_var
from .stdb import Row, ts_micros

log = get_logger("bus")

Where = Callable[[Row], bool]
KINDS = ("insert", "update", "delete", "enter")


class BusClient(Protocol):
    """What the bus needs from a SpacetimeDB client (``stdb.SpacetimeClient`` or a test fake)."""

    state: str
    identity: str | None

    def subscribe(self, queries: list[str]) -> None: ...
    def on_snapshot(self, cb) -> None: ...
    def on_insert(self, cb) -> None: ...
    def on_update(self, cb) -> None: ...
    def on_delete(self, cb) -> None: ...
    def on_state(self, cb) -> None: ...
    def key(self, table: str, row: Row) -> Any: ...
    async def run(self) -> None: ...
    def stop(self) -> None: ...
    async def call_reducer(self, reducer: str, args: list | dict) -> int: ...


# --------------------------------------------------------------------------- registry

@dataclass
class Handler:
    name: str
    table: str
    kind: str
    fn: Callable
    where: Where | None = None
    include_existing: bool = False
    calls: int = 0
    errors: int = 0
    last_error: str | None = None
    last_ms: float | None = None
    last_at: float | None = None

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.fn)

    def stats(self) -> dict:
        return {
            "name": self.name, "table": self.table, "kind": self.kind, "calls": self.calls,
            "errors": self.errors, "last_error": self.last_error, "last_ms": self.last_ms,
            "last_at": _iso(self.last_at) if self.last_at else None,
        }


class Registry:
    def __init__(self) -> None:
        self.handlers: list[Handler] = []
        self.tables: dict[str, str | None] = {}  # table -> optional SQL WHERE for its subscription

    def watch(self, table: str, where_sql: str | None = None) -> None:
        """Subscribe to a table (implicitly done by every decorator). where_sql narrows the query."""
        if where_sql is not None or table not in self.tables:
            self.tables[table] = where_sql

    def queries(self) -> list[str]:
        return [f"SELECT * FROM {t}" + (f" WHERE {w}" if w else "") for t, w in self.tables.items()]

    def _add(self, kind: str, table: str, where: Where | None, include_existing: bool, name: str | None):
        def deco(fn):
            self.watch(table)
            self.handlers.append(Handler(name or f"{fn.__module__}.{fn.__qualname__}", table, kind, fn, where, include_existing))
            return fn
        return deco

    def on_insert(self, table: str, *, where: Where | None = None, include_existing: bool = False, name: str | None = None):
        return self._add("insert", table, where, include_existing, name)

    def on_update(self, table: str, *, where: Where | None = None, name: str | None = None):
        return self._add("update", table, where, False, name)

    def on_delete(self, table: str, *, where: Where | None = None, name: str | None = None):
        return self._add("delete", table, where, False, name)

    def on_enter(self, table: str, where: Where, *, include_existing: bool = True, name: str | None = None):
        return self._add("enter", table, where, include_existing, name)


registry = Registry()
on_insert, on_update, on_delete, on_enter, watch = (
    registry.on_insert, registry.on_update, registry.on_delete, registry.on_enter, registry.watch,
)


# --------------------------------------------------------------------------- events

@dataclass
class Event:
    table: str
    kind: str  # insert | update | delete
    row: Row
    old: Row | None = None
    meta: dict = field(default_factory=dict)
    source: str = "live"  # live | snapshot | resync
    received_at: float = field(default_factory=time.time)
    bus: "Bus | None" = None

    @property
    def server_ts_micros(self) -> int | None:
        ts = self.meta.get("timestamp")
        try:
            return ts_micros(ts) if ts is not None else None
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    @property
    def ticket_id(self) -> Any:
        r = self.row
        if self.table == "ticket":
            return r.get("id")
        tid = r.get("ticket_id")
        if tid:
            return tid
        if self.table == "event_log":
            try:
                p = json.loads(r.get("payload_json") or "{}")
                return p.get("ticket_id") if isinstance(p, dict) else None
            except ValueError:
                return None
        return None


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


def _matches(where: Where | None, row: Row | None) -> bool:
    if row is None:
        return False
    if where is None:
        return True
    try:
        return bool(where(row))
    except Exception:  # noqa: BLE001 - a predicate that can't read the row is a non-match
        return False


# --------------------------------------------------------------------------- bus

class Bus:
    def __init__(self, registry: Registry, client: BusClient, *, workers: int = 8) -> None:
        self.registry, self.client = registry, client
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="handler")
        self.mirror: dict[str, dict[Any, Row]] = {}
        self._synced = False
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._inflight = 0
        self.started_at = time.time()
        self.state = "stopped"
        self.state_since = time.time()
        self.connects = 0
        self.last_error: str | None = None
        self.last_event: dict | None = None
        self.snapshot_at: float | None = None

    # ---- lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        c = self.client
        c.on_state(self._on_state)
        c.on_snapshot(self._on_snapshot)
        c.on_insert(lambda t, r, m: self._emit(Event(t, "insert", r, None, m)))
        c.on_update(lambda t, o, n, m: self._emit(Event(t, "update", n, o, m)))
        c.on_delete(lambda t, r, m: self._emit(Event(t, "delete", r, None, m)))
        c.subscribe(self.registry.queries())
        self._task = asyncio.create_task(c.run(), name="stdb-subscription")
        log.info("bus.start", extra={"fields": {
            "tables": list(self.registry.tables), "handlers": [h.name for h in self.registry.handlers]}})

    async def stop(self) -> None:
        self.client.stop()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception:  # noqa: BLE001
                pass
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ---- queries + writes for handlers
    def rows(self, table: str) -> list[Row]:
        return list(self.mirror.get(table, {}).values())

    def get(self, table: str, key: Any) -> Row | None:
        return self.mirror.get(table, {}).get(key)

    async def call_reducer(self, reducer: str, args: list | dict) -> int:
        return await self.client.call_reducer(reducer, args)

    def call_reducer_threadsafe(self, reducer: str, args: list | dict, timeout: float = 10.0) -> int:
        """For sync handlers on the pool: run call_reducer on the bus loop and wait for the send."""
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(self.call_reducer(reducer, args), self._loop).result(timeout)

    # ---- client callbacks (event loop)
    def _on_state(self, state: str, detail: str | None) -> None:
        if state != self.state:
            self.state, self.state_since = state, time.time()
            if state == "live":
                self.connects += 1
            lvl = log.warning if state == "reconnecting" else log.info
            lvl("subscription.state", extra={"fields": {"state": state, "detail": detail}})
        if detail:
            self.last_error = detail

    def _on_snapshot(self, tables: dict[str, list[Row]]) -> None:
        self.snapshot_at = time.time()
        fresh: dict[str, dict[Any, Row]] = {
            t: {self.client.key(t, r): r for r in tables.get(t, [])} for t in self.registry.tables
        }
        counts = {t: len(v) for t, v in fresh.items()}
        if not self._synced:
            # Boot: rebuild state from the DB, then let include_existing handlers pick up in-flight work.
            self.mirror, self._synced = fresh, True
            log.info("subscription.snapshot", extra={"fields": {"rows": counts, "resync": False}})
            for t, rows in fresh.items():
                for r in rows.values():
                    self._fire(Event(t, "insert", r, source="snapshot", bus=self), existing=True)
            return
        # Reconnect: replay what changed while we were away.
        diff: list[Event] = []
        for t, new in fresh.items():
            old = self.mirror.get(t, {})
            for k, r in new.items():
                if k not in old:
                    diff.append(Event(t, "insert", r, source="resync"))
                elif old[k] != r:
                    diff.append(Event(t, "update", r, old[k], source="resync"))
            diff += [Event(t, "delete", r, source="resync") for k, r in old.items() if k not in new]
        self.mirror = fresh
        log.info("subscription.snapshot", extra={"fields": {"rows": counts, "resync": True, "changes": len(diff)}})
        for ev in diff:
            self._emit(ev)

    def _emit(self, ev: Event) -> None:
        ev.bus = self
        if ev.table not in self.registry.tables:
            return
        m = self.mirror.setdefault(ev.table, {})
        k = self.client.key(ev.table, ev.row)
        if ev.kind == "delete":
            m.pop(k, None)
        else:
            m[k] = ev.row
        self.last_event = {"at": time.time(), "table": ev.table, "kind": ev.kind, "source": ev.source,
                           "server_ts_micros": ev.server_ts_micros}
        self._fire(ev)

    def _fire(self, ev: Event, existing: bool = False) -> None:
        for h in self.registry.handlers:
            if h.table != ev.table or not self._wants(h, ev, existing):
                continue
            self._dispatch(h, ev)

    @staticmethod
    def _wants(h: Handler, ev: Event, existing: bool) -> bool:
        if existing:
            return h.include_existing and h.kind in ("insert", "enter") and _matches(h.where, ev.row)
        if h.kind == "enter":
            if ev.kind == "insert":
                return _matches(h.where, ev.row)
            if ev.kind == "update":
                return _matches(h.where, ev.row) and not _matches(h.where, ev.old)
            return False
        return h.kind == ev.kind and _matches(h.where, ev.row)

    # ---- dispatch (worker pool for sync handlers, loop tasks for async ones)
    def _dispatch(self, h: Handler, ev: Event) -> None:
        with self._lock:
            self._inflight += 1
        if h.is_async:
            asyncio.get_running_loop().create_task(self._run_async(h, ev))
        else:
            ctx = contextvars.copy_context()
            self.pool.submit(ctx.run, self._run_sync, h, ev)

    def _begin(self, h: Handler, ev: Event):
        tok = ticket_id_var.set(ev.ticket_id)
        return tok, time.time()

    def _end(self, h: Handler, ev: Event, tok, t0: float, err: BaseException | None) -> None:
        ms = round((time.time() - t0) * 1000, 2)
        sts = ev.server_ts_micros
        lag = round(t0 * 1000 - sts / 1000, 1) if sts else None  # server commit -> handler start
        with self._lock:
            self._inflight -= 1
            h.calls += 1
            h.last_ms, h.last_at = ms, time.time()
            if err is not None:
                h.errors += 1
                h.last_error = f"{type(err).__name__}: {err}"
        fields = {"handler": h.name, "table": ev.table, "kind": ev.kind, "source": ev.source,
                  "ms": ms, "lag_ms": lag}
        if err is None:
            log.info("handler.ok", extra={"fields": fields})
        else:
            log.error("handler.error", exc_info=(type(err), err, err.__traceback__), extra={"fields": fields})
        ticket_id_var.reset(tok)

    def _run_sync(self, h: Handler, ev: Event) -> None:
        tok, t0 = self._begin(h, ev)
        err = None
        try:
            h.fn(ev)
        except Exception as e:  # noqa: BLE001 - counted and logged, never kills the pool
            err = e
        self._end(h, ev, tok, t0, err)

    async def _run_async(self, h: Handler, ev: Event) -> None:
        tok, t0 = self._begin(h, ev)
        err = None
        try:
            await h.fn(ev)
        except Exception as e:  # noqa: BLE001
            err = e
        self._end(h, ev, tok, t0, err)

    # ---- health
    def health(self) -> dict:
        now = time.time()
        le = self.last_event
        with self._lock:
            handlers = [h.stats() for h in self.registry.handlers]
            inflight = self._inflight
        return {
            "status": "ok" if self.state == "live" else "degraded",
            "uptime_s": round(now - self.started_at, 1),
            "subscription": {
                "state": self.state,
                "since": _iso(self.state_since),
                "connects": self.connects,
                "reconnects": max(self.connects - 1, 0),
                "identity": self.client.identity,
                "tables": list(self.registry.tables),
                "rows": {t: len(v) for t, v in self.mirror.items()},
                "last_snapshot_at": _iso(self.snapshot_at) if self.snapshot_at else None,
                "last_error": self.last_error,
            },
            "last_event": None if le is None else {
                "at": _iso(le["at"]), "age_s": round(now - le["at"], 1), "table": le["table"],
                "kind": le["kind"], "source": le["source"], "server_ts_micros": le["server_ts_micros"],
            },
            "handler_errors": sum(h["errors"] for h in handlers),
            "handlers": handlers,
            "inflight": inflight,
        }
