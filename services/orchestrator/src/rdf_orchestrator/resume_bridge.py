"""TDM-27 resume bridge: a SpacetimeDB real-time update wakes a paused Factory agent.

A droid that needs the physical world parks itself with ``agent_wait_for(session_id, predicate_json,
resume_payload_json)`` (TDM-13), so its ``agent_session`` row is ``status = 'waiting_for'`` with the
predicate in ``waiting_for_json``. This bridge, running inside the orchestrator bus, does:

  1. watch ``agent_session`` rows that are ``waiting_for`` (bus handlers, ``handlers/resume_bridge.py``);
  2. read each predicate and index the tables it names. Tables the main bus already mirrors are used
     directly; any other table gets a side subscription that this module opens and grows on demand;
  3. evaluate the predicate when the session starts waiting (it may already be true), on every
     insert/update/delete of a named table, on boot and on every resync (the bus replays changes as
     events), and on a safety poll every ``RESUME_BRIDGE_POLL_S`` (30 s);
  4. claim the wake-up with ``agent_resume(session_id, "", reason)``. The reducer only succeeds from
     ``waiting_for``/``paused``, so when two bridges race, only one wins. The loser sees the reducer
     error and does nothing;
  5. the winner calls the Factory client: ``resumer.resume(agent_session_id, text)`` (TDM-30,
     ``rdf_factory``), where ``text`` says what changed. If the client fell back to Protocol B (new
     Factory session seeded with ``resume_payload``) and reports the new session id, the bridge records
     it with ``upsert_agent_session``, which appends it to ``factory_session_ids_json`` under the same
     ``agent_session.id`` (the "claim first, report the id after" path in spacetime/schema.md);
  6. log ``agent.resume_delivering`` when the Factory call starts, then ``agent.resume_delivered``
     {predicate, elapsed_s, detect_ms, mode, ...} (or ``agent.resume_failed``) to event_log.
     ``agent_resume`` itself logs ``agent.resumed`` with the satisfied predicate.

Predicate semantics (``parse_predicate`` / ``evaluate``):
  * a leaf is ``{"table": T, "field": F, "op": OP, "value": V, ...filters}``. Every other key (e.g.
    ``ticket_id``, ``id``, ``kind``), plus an optional ``"where": {col: val}`` object, is an equality
    filter on T's rows. The leaf is true if ANY filtered row has ``row[F] OP V``.
  * ``op`` is one of ``== != > >= < <=`` (default ``==``). Numbers compare numerically (a numeric string
    is coerced), anything else compares by equality only. Incomparable values are false, never an
    error.
  * ``"agg": "count"`` compares the number of filtered rows (``field`` not needed). ``"agg": "sum" |
    "min" | "max"`` aggregates ``field`` over the filtered rows. A leaf with no ``field`` and no ``agg``
    is "some filtered row exists".
  * ``{"all": [...]}`` / ``{"any": [...]}`` nest (TDM-13 validates depth <= 4).
  * A predicate naming an unknown table, or a ``ticket_id`` that has no ticket row, is logged as an
    error (and ``agent.wait_error`` once per wait). The session keeps waiting; nothing crashes.

Restart and race safety: nothing here is trusted across a restart. On boot, the bus snapshot fires
the session handler for every waiting row and the predicate is evaluated against the fresh mirror.
Claims are made only through the reducer, and an in-process ``(session_id, last_update)`` key stops one
bridge from claiming the same wait twice. A claim whose delivery never happened (the process died
between ``agent_resume`` and the Factory call) is found by the poll: the session is still
``resuming`` after ``RESUME_BRIDGE_ORPHAN_S`` and has an ``agent.resumed`` event but no
``agent.resume_delivering``/``delivered``/``failed`` after it. The bridge then delivers it.

If no Factory client can be loaded, the bridge logs ``resume_bridge.no_resumer`` and never claims, so
a session is never woken without a message.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import operator
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from .log import get_logger

log = get_logger("resume_bridge")

ACTOR = "orchestrator"
EV_DELIVERED = "agent.resume_delivered"
EV_FAILED = "agent.resume_failed"
EV_DELIVERING = "agent.resume_delivering"
EV_WAIT_ERROR = "agent.wait_error"

# Tables the bridge mirrors on the main bus: everything the other handlers already watch, plus
# agent_session and qa_result. A predicate on any other table gets a side subscription.
BUS_TABLES = (
    "agent_session", "ticket", "run_settings", "robot", "dataset", "failure_cluster", "data_plan", "task",
    "episode", "review", "qa_result", "payment", "dataset_offer", "training_run", "eval_run", "eval_trial",
    "robot_command", "contributor", "event_log",
)

# "raw" is the droid's original WAITING_FOR line (TDM-30 keeps it for display); "predicate" is
# TDM-30's free-text fallback, which cannot be evaluated.
LEAF_RESERVED = {"table", "field", "op", "value", "agg", "where", "raw", "predicate"}
OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le,
}
AGGS = ("count", "sum", "min", "max")


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# =========================================================================== predicates

class PredicateError(ValueError):
    pass


@dataclass(frozen=True)
class Leaf:
    table: str
    field: str | None
    op: str
    value: Any
    filters: tuple[tuple[str, Any], ...] = ()
    agg: str | None = None

    def describe(self) -> str:
        lhs = f"{self.agg}({self.table}{'.' + self.field if self.field else ''})" if self.agg else (
            f"{self.table}.{self.field}" if self.field else f"exists {self.table}")
        rhs = "" if (self.field is None and self.agg is None) else f" {self.op} {self.value!r}"
        flt = f" [{', '.join(f'{k}={v!r}' for k, v in self.filters)}]" if self.filters else ""
        return lhs + rhs + flt


@dataclass(frozen=True)
class Group:
    kind: str  # all | any
    children: tuple["Node", ...]

    def describe(self) -> str:
        j = " AND " if self.kind == "all" else " OR "
        return "(" + j.join(c.describe() for c in self.children) + ")"


Node = Leaf | Group


def parse_predicate(obj: Any, _depth: int = 0) -> Node:
    """JSON text or object -> Leaf/Group. Raises PredicateError on a malformed predicate."""
    if isinstance(obj, (str, bytes)):
        try:
            obj = json.loads(obj)
        except ValueError as e:
            raise PredicateError(f"predicate is not valid JSON: {e}") from None
    if _depth > 8:
        raise PredicateError("predicate nested too deeply")
    if not isinstance(obj, dict):
        raise PredicateError("predicate must be a JSON object")
    for kind in ("all", "any"):
        if kind in obj:
            kids = obj[kind]
            if not isinstance(kids, list) or not kids:
                raise PredicateError(f"predicate.{kind} must be a non-empty list")
            return Group(kind, tuple(parse_predicate(k, _depth + 1) for k in kids))
    table = obj.get("table")
    if not isinstance(table, str) or not table:
        raise PredicateError('predicate needs a "table" (or "all"/"any")')
    op = str(obj.get("op", "=="))
    if op not in OPS:
        raise PredicateError(f"predicate.op {op!r} is not one of {' '.join(OPS)}")
    agg = obj.get("agg")
    if agg is not None and agg not in AGGS:
        raise PredicateError(f"predicate.agg {agg!r} is not one of {', '.join(AGGS)}")
    fld = obj.get("field")
    if "predicate" in obj and not fld and not obj.get("agg"):
        raise PredicateError(f"free-text predicate cannot be evaluated: {str(obj.get('raw') or obj['predicate'])[:200]}"
                             " (use WAITING_FOR <table>:<field><op><value> or a JSON predicate)")
    if fld is not None and not isinstance(fld, str):
        raise PredicateError("predicate.field must be a string")
    if agg in ("sum", "min", "max") and not fld:
        raise PredicateError(f"predicate.agg {agg} needs a field")
    if (fld or agg) and "value" not in obj:
        raise PredicateError("predicate needs a value to compare with")
    where = obj.get("where") or {}
    if not isinstance(where, dict):
        raise PredicateError("predicate.where must be an object")
    filters = {k: v for k, v in obj.items() if k not in LEAF_RESERVED}
    filters.update(where)
    return Leaf(table, fld or None, op, obj.get("value"), tuple(sorted(filters.items())), agg)


def tables_of(node: Node) -> set[str]:
    if isinstance(node, Leaf):
        return {node.table}
    return set().union(*(tables_of(c) for c in node.children))


def leaves_of(node: Node) -> list[Leaf]:
    if isinstance(node, Leaf):
        return [node]
    return [x for c in node.children for x in leaves_of(c)]


def _num(v: Any) -> float | int | None:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v) if any(ch in v for ch in ".eE") else int(v)
        except ValueError:
            return None
    return None


def compare(a: Any, op: str, b: Any) -> bool:
    """``a op b`` with light coercion. Never raises: incomparable values are simply false."""
    fn = OPS[op]
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None and not (isinstance(a, str) and isinstance(b, str)):
        return bool(fn(na, nb))
    if op in ("==", "!="):
        return bool(fn(a, b))
    try:
        return bool(fn(a, b))
    except TypeError:
        return False


def filter_rows(leaf: Leaf, rows: Iterable[dict]) -> list[dict]:
    return [r for r in rows if all(k in r and compare(r.get(k), "==", v) for k, v in leaf.filters)]


@dataclass
class Evaluation:
    ok: bool
    matched: list[tuple[Leaf, list[dict]]] = field(default_factory=list)  # leaves that held, with rows
    missing: set[str] = field(default_factory=set)  # tables with no data available yet
    empty: list[Leaf] = field(default_factory=list)  # leaves whose filters matched no row


RowsFor = Callable[[str], "list[dict] | None"]


def evaluate(node: Node, rows_for: RowsFor) -> Evaluation:
    """Evaluate against ``rows_for(table)`` (None = table not available yet -> that leaf is false)."""
    ev = Evaluation(False)
    ev.ok = _eval(node, rows_for, ev)
    return ev


def _eval(node: Node, rows_for: RowsFor, ev: Evaluation) -> bool:
    if isinstance(node, Group):
        results = [_eval(c, rows_for, ev) for c in node.children]  # evaluate all: full diagnostics
        return all(results) if node.kind == "all" else any(results)
    rows = rows_for(node.table)
    if rows is None:
        ev.missing.add(node.table)
        return False
    sel = filter_rows(node, rows)
    if not sel and node.agg != "count":
        ev.empty.append(node)
    if node.agg == "count":
        ok, hit = compare(len(sel), node.op, node.value), sel
    elif node.agg:
        vals = [_num(r.get(node.field)) for r in sel]
        vals = [v for v in vals if v is not None]
        if not vals:
            return False
        agg = {"sum": sum, "min": min, "max": max}[node.agg](vals)
        ok, hit = compare(agg, node.op, node.value), sel
    elif node.field is None:
        ok, hit = bool(sel), sel
    else:
        hit = [r for r in sel if node.field in r and compare(r.get(node.field), node.op, node.value)]
        ok = bool(hit)
    if ok:
        ev.matched.append((node, hit))
    return ok


# =========================================================================== Factory client seam

class Resumer(Protocol):
    """What the bridge needs from the Factory client (TDM-30 ``rdf_factory.FactoryClient``).

    ``resume(agent_session_id, text)`` messages the agent's Factory session (Protocol A) or, if that
    session or its computer is gone, starts a new one seeded with ``resume_payload`` (Protocol B). The
    client adds the ``[resume-bridge]`` prefix the droid contract looks for. It may be sync or async.
    It may return None, or anything (dict or object) that says what happened:
      * TDM-30 ``TurnResult``: ``protocol`` (native_resume | restart), ``factory_session_id``, ``status``,
        ``ok``. That client records a restart's new session id itself (``upsert_agent_session``).
      * anything else: ``new_factory_session_id`` (or ``mode == "restart"`` plus ``factory_session_id``);
        the bridge then records the new id with ``upsert_agent_session``.
    """

    def resume(self, agent_session_id: int, text: str) -> Any: ...


class Writer(Protocol):
    def call_reducer(self, name: str, /, *args: Any) -> None: ...


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def restart_info(result: Any, current: str | None) -> tuple[str | None, bool]:
    """(new Factory session id or None for a native resume, whether the bridge must record it)."""
    if _attr(result, "protocol") is not None:  # TDM-30 TurnResult: the client mirrored it already
        sid = _attr(result, "factory_session_id") if _attr(result, "protocol") == "restart" else None
        return (str(sid) if sid and str(sid) != (current or "") else None), False
    sid = _attr(result, "new_factory_session_id")
    if not sid and _attr(result, "mode") == "restart":
        sid = _attr(result, "factory_session_id")
    sid = str(sid) if sid and str(sid) != (current or "") else None
    return sid, sid is not None


def new_session_id(result: Any, current: str | None) -> str | None:
    return restart_info(result, current)[0]


class _SyncResumer:
    """Adapts a resumer to the bridge's sync call: an async ``resume`` runs on one private event loop
    thread (so a client holding loop-bound HTTP connections always sees the same loop), and the
    TDM-30 keywords are passed when the signature takes them (``claim=False``: the bridge has already
    claimed with ``agent_resume``)."""

    def __init__(self, inner: Any) -> None:
        import inspect

        self.inner = inner
        fn = inner.resume
        self.is_async = inspect.iscoroutinefunction(fn)
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        self.kw = {k: v for k, v in (("claim", False),) if k in params}
        self.takes_reason = "reason" in params
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="resume-bridge-factory", daemon=True).start()
                self._loop = loop
            return self._loop

    def resume(self, agent_session_id: int, text: str, reason: str = "") -> Any:
        kw = dict(self.kw)
        if self.takes_reason and reason:
            kw["reason"] = reason
        if self.is_async:
            fut = asyncio.run_coroutine_threadsafe(self.inner.resume(agent_session_id, text, **kw), self._get_loop())
            return fut.result()
        return self.inner.resume(agent_session_id, text, **kw)


def load_resumer() -> Resumer | None:
    """``RESUME_BRIDGE_RESUMER=pkg.mod:attr`` or the TDM-30 client in ``rdf_factory``; None if absent."""
    spec = os.environ.get("RESUME_BRIDGE_RESUMER", "").strip()
    candidates = [spec] if spec else ["rdf_factory:FactoryClient", "rdf_factory.client:FactoryClient"]
    for c in candidates:
        mod_name, _, attr = c.partition(":")
        try:
            obj = getattr(importlib.import_module(mod_name), attr or "FactoryClient")
            if isinstance(obj, type):
                obj = obj.from_env() if hasattr(obj, "from_env") else obj()
        except (ImportError, AttributeError):
            continue
        except Exception as e:  # noqa: BLE001 - a broken client must not take the bus down
            log.error("resume_bridge.resumer_import_failed", extra={"fields": {"spec": c, "error": repr(e)[:300]}})
            continue
        if callable(obj) and not hasattr(obj, "resume"):
            fn = obj

            class _Fn:
                def resume(self, agent_session_id: int, text: str) -> Any:
                    return fn(agent_session_id, text)

            obj = _Fn()
        if hasattr(obj, "resume"):
            log.info("resume_bridge.resumer", extra={"fields": {"spec": c, "type": type(obj).__name__}})
            return obj
    return None


def _default_writer() -> Writer:
    from rdf_common.settings import SpacetimeSettings, load_settings
    from rdf_common.spacetime_client import SpacetimeClient

    return SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"), timeout=15.0)


def _default_side_client(bus: Any, queries: list[str]) -> Any:
    from .stdb import SpacetimeClient

    c = bus.client
    side = SpacetimeClient(host=c.host, db=c.db, token=c.token, max_backoff=10.0)
    side.subscribe(queries)
    return side


# swappable for tests / proofs
make_writer: Callable[[], Writer] = _default_writer
make_resumer: Callable[[], Resumer | None] = load_resumer
make_side_client: Callable[[Any, list[str]], Any] = _default_side_client


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _json(v: Any) -> Any:
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def _short(v: Any, n: int = 80) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 3] + "..."


# =========================================================================== side subscription

class SideTables:
    """A second subscription for predicate tables the main bus does not mirror. It grows on demand:
    a new table means a new subscription with the larger query set (SpacetimeDB's legacy Subscribe
    replaces the whole set). Its snapshots and row events feed ``bridge.table_changed``."""

    def __init__(self, bridge: "ResumeBridge") -> None:
        self.b = bridge
        self.tables: set[str] = set()
        self.cache: dict[str, dict[Any, dict]] = {}
        self.ready: set[str] = set()
        self._client: Any = None
        self._task: asyncio.Future | None = None
        self._lock = threading.Lock()

    def rows(self, table: str) -> list[dict] | None:
        with self._lock:
            return list(self.cache[table].values()) if table in self.ready else None

    def ensure(self, tables: set[str]) -> None:
        with self._lock:
            new = tables - self.tables
            if not new:
                return
            self.tables |= new
            want = sorted(self.tables)
        loop = getattr(self.b.bus, "_loop", None)
        if loop is None:
            log.warning("resume_bridge.side_no_loop", extra={"fields": {"tables": want}})
            return
        log.info("resume_bridge.side_subscribe", extra={"fields": {"tables": want, "added": sorted(new)}})
        asyncio.run_coroutine_threadsafe(self._restart(want), loop)

    async def _restart(self, tables: list[str]) -> None:
        old, oldtask = self._client, self._task
        if old is not None:
            old.stop()
            if oldtask is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(oldtask), timeout=3)
                except Exception:  # noqa: BLE001
                    pass
        c = make_side_client(self.b.bus, [f"SELECT * FROM {t}" for t in tables])
        c.on_state(lambda s, d: log.info("resume_bridge.side_state", extra={"fields": {"state": s, "detail": d}})
                   if s != "live" or d else None)
        c.on_snapshot(self._snapshot)
        c.on_insert(lambda t, r, m: self._row(t, r, None, m, "insert"))
        c.on_update(lambda t, o, n, m: self._row(t, n, o, m, "update"))
        c.on_delete(lambda t, r, m: self._row(t, r, None, m, "delete"))
        self._client = c
        self._task = asyncio.ensure_future(c.run())

    def _snapshot(self, tables: dict[str, list[dict]]) -> None:
        c = self._client
        with self._lock:
            for t in self.tables:
                self.cache[t] = {c.key(t, r): r for r in tables.get(t, [])}
                self.ready.add(t)
        for t in list(self.tables):
            self.b.table_changed(t, Change(t, "snapshot", None, None, None))

    def _row(self, table: str, row: dict, old: dict | None, meta: dict, kind: str) -> None:
        c = self._client
        with self._lock:
            m = self.cache.setdefault(table, {})
            k = c.key(table, row)
            if kind == "delete":
                m.pop(k, None)
            else:
                m[k] = row
        ts = None
        try:
            from .stdb import ts_micros

            ts = ts_micros(meta.get("timestamp")) if meta and meta.get("timestamp") is not None else None
        except Exception:  # noqa: BLE001
            ts = None
        self.b.table_changed(table, Change(table, kind, row, old, ts))

    def stop(self) -> None:
        if self._client is not None:
            self._client.stop()


# =========================================================================== the bridge

@dataclass
class Change:
    """What triggered an evaluation (used in the resume message and the event)."""

    table: str
    kind: str  # insert | update | delete | snapshot | resync | poll | wait
    row: dict | None
    old: dict | None
    server_ts_us: int | None = None

    def describe(self, fields: set[str]) -> str:
        if self.kind in ("poll", "wait", "snapshot", "boot"):
            return {"poll": "found true on the safety poll", "wait": "already true when the wait started",
                    "snapshot": f"found true when the {self.table} snapshot loaded",
                    "boot": "found true on orchestrator boot"}[self.kind]
        r = self.row or {}
        ident = f"{self.table} {r.get('id', '?')}"
        if self.kind == "update" and self.old is not None:
            diffs = [f"{f}: {_short(self.old.get(f), 40)} -> {_short(r.get(f), 40)}" for f in sorted(fields)
                     if f in r and self.old.get(f) != r.get(f)]
            return f"{ident} updated" + (f" ({'; '.join(diffs)})" if diffs else "")
        return f"{ident} {self.kind}ed".replace("deleteed", "deleted")


@dataclass
class Waiting:
    session_id: int
    gen: int  # last_update (us) of the waiting row: one wait = one claim
    row: dict
    raw: Any
    pred: Node | None
    tables: set[str]
    error: str | None = None
    reported: set[str] = field(default_factory=set)  # error/warning keys already logged this wait


class ResumeBridge:
    def __init__(self, bus: Any, *, writer_factory: Callable[[], Writer] | None = None,
                 resumer_factory: Callable[[], Resumer | None] | None = None, poll_s: float | None = None,
                 orphan_s: float | None = None, retry_delays: tuple[float, ...] = (1.0, 3.0, 9.0),
                 bridge_id: str | None = None) -> None:
        self.bus = bus
        self._writer_factory = writer_factory or (lambda: make_writer())
        self._resumer_factory = resumer_factory or (lambda: make_resumer())
        self._writer: Writer | None = None
        self._resumer: Resumer | None = None
        self._resumer_loaded = False
        self.poll_s = poll_s if poll_s is not None else _env_f("RESUME_BRIDGE_POLL_S", 30.0)
        self.orphan_s = orphan_s if orphan_s is not None else _env_f("RESUME_BRIDGE_ORPHAN_S", 180.0)
        self.retry_delays = retry_delays
        self.bridge_id = bridge_id or f"{socket.gethostname()}:{os.getpid()}"
        self.side = SideTables(self)
        self.waiting: dict[int, Waiting] = {}
        self.by_table: dict[str, set[int]] = {}
        self.outcomes: dict[tuple[int, int], str] = {}  # (session, gen) -> claimed|lost|...
        self.inflight: set[tuple[int, int]] = set()
        self.recovered: set[int] = set()  # agent.resumed event ids already re-delivered
        self._lock = threading.RLock()
        self._wlock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="resume-bridge")
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self._no_resumer_logged = False
        self.stats = {"evaluations": 0, "claims": 0, "lost": 0, "delivered": 0, "failed": 0, "recovered": 0}

    # ------------------------------------------------------------------ plumbing
    @property
    def writer(self) -> Writer:
        with self._wlock:
            if self._writer is None:
                self._writer = self._writer_factory()
            return self._writer

    def resumer(self) -> "_SyncResumer | None":
        with self._wlock:
            if not self._resumer_loaded:
                r = self._resumer_factory()
                self._resumer, self._resumer_loaded = (_SyncResumer(r) if r is not None else None), True
            return self._resumer

    def _bus_rows(self, table: str) -> list[dict]:
        for _ in range(5):
            try:
                return list(self.bus.rows(table))
            except RuntimeError:  # mirror dict resized under us (other thread): retry
                time.sleep(0.001)
        return list(self.bus.rows(table))

    def on_bus(self, table: str) -> bool:
        return table in getattr(getattr(self.bus, "registry", None), "tables", {})

    def rows_for(self, table: str) -> list[dict] | None:
        if self.on_bus(table):
            return self._bus_rows(table)
        return self.side.rows(table)

    def known_tables(self) -> set[str] | None:
        schema = getattr(getattr(self.bus, "client", None), "schema", None)
        if isinstance(schema, dict) and schema:
            return set(schema)
        return None

    def event(self, type_: str, payload: dict) -> None:
        try:
            self.writer.call_reducer("append_event", ACTOR, type_, json.dumps(payload, default=str))
        except Exception as e:  # noqa: BLE001 - the structured log line is the fallback record
            log.warning("resume_bridge.event_failed", extra={"fields": {"type": type_, "error": str(e)[:300]}})

    def _report_once(self, w: Waiting, key: str, level: str, msg: str, fields: dict, event: bool) -> None:
        if key in w.reported:
            return
        w.reported.add(key)
        f = {"session_id": w.session_id, "ticket_id": _i(w.row.get("ticket_id")), **fields}
        getattr(log, level)(msg, extra={"fields": f})
        if event:
            self.pool.submit(self.event, EV_WAIT_ERROR, {**f, "predicate": w.raw, "bridge": self.bridge_id})

    # ------------------------------------------------------------------ session tracking
    def session_changed(self, row: dict, change: Change | None = None) -> None:
        sid = _i(row.get("id"))
        if row.get("status") != "waiting_for" or not row.get("waiting_for_json"):
            with self._lock:
                self._drop(sid)
            return
        gen = _i(row.get("last_update"))
        fresh = False
        with self._lock:
            cur = self.waiting.get(sid)
            if cur is not None and cur.gen == gen:
                cur.row = row
                w = cur
            else:
                self._drop(sid)
                w, fresh = self._track(sid, gen, row), True
        if fresh and (change is None or change.kind not in ("boot", "poll")):
            change = Change("agent_session", "wait", row, None, None)  # it may be true already
        if w.pred is not None:
            self.check(sid, change or Change("agent_session", "wait", row, None, None))

    def session_deleted(self, row: dict) -> None:
        with self._lock:
            self._drop(_i(row.get("id")))

    def _drop(self, sid: int) -> None:
        w = self.waiting.pop(sid, None)
        if w is not None:
            for t in w.tables:
                s = self.by_table.get(t)
                if s is not None:
                    s.discard(sid)
                    if not s:
                        self.by_table.pop(t, None)

    def _track(self, sid: int, gen: int, row: dict) -> Waiting:
        raw = _json(row.get("waiting_for_json"))
        try:
            pred = parse_predicate(raw if raw is not None else row.get("waiting_for_json"))
            tables, err = tables_of(pred), None
        except PredicateError as e:
            pred, tables, err = None, set(), str(e)
        w = Waiting(sid, gen, row, raw, pred, tables, err)
        self.waiting[sid] = w
        if err:
            self._report_once(w, "parse", "error", "resume_bridge.bad_predicate", {"error": err}, True)
            return w
        known = self.known_tables()
        unknown = {t for t in tables if known is not None and t not in known}
        if unknown:
            self._report_once(w, "unknown_table", "error", "resume_bridge.unknown_table",
                              {"tables": sorted(unknown), "error": f"no such table: {', '.join(sorted(unknown))}"}, True)
        for t in tables:
            self.by_table.setdefault(t, set()).add(sid)
        side = {t for t in tables if not self.on_bus(t) and t not in unknown}
        if side:
            self.side.ensure(side)
        log.info("resume_bridge.tracking", extra={"fields": {
            "session_id": sid, "ticket_id": _i(row.get("ticket_id")), "predicate": pred.describe(),
            "tables": sorted(tables), "side_tables": sorted(side)}})
        return w

    # ------------------------------------------------------------------ evaluation
    def table_changed(self, table: str, change: Change) -> None:
        with self._lock:
            sids = list(self.by_table.get(table, ()))
        for sid in sids:
            self.check(sid, change)

    def check(self, sid: int, change: Change) -> str:
        """Evaluate one waiting session; on true, submit the claim. Returns what happened."""
        with self._lock:
            w = self.waiting.get(sid)
            if w is None or w.pred is None:
                return "not_waiting"
            key = (sid, w.gen)
            if key in self.outcomes or key in self.inflight:
                return "handled"
        self.stats["evaluations"] += 1
        res = evaluate(w.pred, self.rows_for)
        self._diagnose(w, res)
        if not res.ok:
            return "false"
        if self.resumer() is None:
            if not self._no_resumer_logged:
                self._no_resumer_logged = True
                log.error("resume_bridge.no_resumer", extra={"fields": {
                    "session_id": sid, "note": "no Factory client (rdf_factory / RESUME_BRIDGE_RESUMER): not claiming"}})
            return "no_resumer"
        with self._lock:
            if key in self.outcomes or key in self.inflight or self.waiting.get(sid) is not w:
                return "handled"
            self.inflight.add(key)
        self.pool.submit(self._claim_and_deliver, w, res, change, time.time())
        return "claiming"

    def _diagnose(self, w: Waiting, res: Evaluation) -> None:
        for leaf in res.empty:
            tid = dict(leaf.filters).get("ticket_id")
            if tid is not None and self.on_bus("ticket") and getattr(self.bus, "_synced", True):
                if not any(compare(t.get("id"), "==", tid) for t in self._bus_rows("ticket")):
                    self._report_once(w, f"ticket:{tid}", "error", "resume_bridge.unknown_ticket",
                                      {"leaf": leaf.describe(), "error": f"ticket {tid} does not exist"}, True)
                    continue
            self._report_once(w, f"empty:{leaf.describe()}", "warning", "resume_bridge.no_rows",
                              {"leaf": leaf.describe(), "note": "no row matches the predicate's filters yet"}, False)

    # ------------------------------------------------------------------ claim + deliver
    def _claim_and_deliver(self, w: Waiting, res: Evaluation, change: Change, t_true: float) -> None:
        key = (w.session_id, w.gen)
        sid = w.session_id
        elapsed_s = round(max(0, _now_us() - w.gen) / 1e6, 3) if w.gen else None
        desc = w.pred.describe() if w.pred else "?"
        changed = change.describe({l.field for l in leaves_of(w.pred) if l.field} if w.pred else set())
        reason = f"predicate true: {desc}; {changed}; waited {elapsed_s}s (bridge {self.bridge_id})"[:900]
        t0 = time.time()
        try:
            self.writer.call_reducer("agent_resume", sid, "", reason)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            refused = "unauthorized" in msg
            lost = not refused and ("not waiting_for" in msg or "unknown agent_session" in msg
                                    or getattr(e, "status", None) == 530)
            with self._lock:
                self.inflight.discard(key)
                if lost or refused:
                    self.outcomes[key] = "lost" if lost else "refused"
            if lost:  # another bridge (or a human) moved the session first: that is the race working
                self.stats["lost"] += 1
                log.info("resume_bridge.lost_race", extra={"fields": {"session_id": sid, "error": msg[:300]}})
            elif refused:
                log.error("resume_bridge.claim_refused", extra={"fields": {
                    "session_id": sid, "error": msg[:300], "note": "the bridge identity needs the orchestrator role"}})
            else:  # transport error: leave it for the next event / poll
                log.warning("resume_bridge.claim_error", extra={"fields": {"session_id": sid, "error": msg[:300]}})
            return
        t_claim = time.time()
        with self._lock:
            self.outcomes[key] = "claimed"
            self.inflight.discard(key)
        self.stats["claims"] += 1
        detect_ms = round(t_claim * 1000 - change.server_ts_us / 1000, 1) if change.server_ts_us else None
        log.info("resume_bridge.claimed", extra={"fields": {
            "session_id": sid, "predicate": desc, "changed": changed, "elapsed_s": elapsed_s,
            "claim_ms": round((t_claim - t0) * 1000, 1), "detect_ms": detect_ms}})
        text = self.message(w, res, changed, elapsed_s)
        self.deliver(w.row, text, reason=reason, predicate=w.raw, predicate_text=desc, changed=changed, elapsed_s=elapsed_s,
                     detect_ms=detect_ms, true_to_claim_ms=round((t_claim - t_true) * 1000, 1),
                     matched=self._matched(res))

    def deliver(self, row: dict, text: str, **info: Any) -> bool:
        """Hand the wake-up to the Factory client (after a won claim, or an orphan recovery).

        Transport errors (ConnectionError / TimeoutError / OSError) are retried; anything else fails
        at once, because the client may already have messaged the droid. A conflict from the client
        (the row moved on) is logged and left alone. Any other failure parks the session as
        ``paused`` (``agent_resume`` works from paused) and logs ``agent.resume_failed``.
        """
        sid = _i(row.get("id"))
        resumer = self.resumer()
        base = {"session_id": sid, "ticket_id": _i(row.get("ticket_id")), "role": row.get("role"),
                "bridge": self.bridge_id, **info}
        # Marks "a live bridge has it" for orphan recovery; the Factory turn can take minutes.
        self.event(EV_DELIVERING, {k: base.get(k) for k in ("session_id", "ticket_id", "bridge", "recovered")})
        t0 = time.time()
        result, err = None, None
        for attempt, delay in enumerate((0.0, *self.retry_delays)):
            if delay:
                time.sleep(delay)
            try:
                result, err = resumer.resume(sid, text, reason=info.get("reason", "")), None  # type: ignore[union-attr]
                break
            except Exception as e:  # noqa: BLE001
                err = e
                log.warning("resume_bridge.resume_error", extra={"fields": {
                    "session_id": sid, "attempt": attempt + 1, "error": repr(e)[:300]}})
                if not isinstance(e, (ConnectionError, TimeoutError, OSError)):
                    break
        base["deliver_ms"] = round((time.time() - t0) * 1000, 1)
        if err is not None:
            self.stats["failed"] += 1
            note = f"resume failed: {type(err).__name__}: {err}"[:500]
            if type(err).__name__ != "ResumeConflict":
                try:  # resuming -> paused: a human (or agent_resume from paused) can retry it
                    self.writer.call_reducer("upsert_agent_session", sid, row.get("role"), "", "paused", note, "", 0)
                except Exception as e:  # noqa: BLE001
                    log.warning("resume_bridge.pause_failed", extra={"fields": {"session_id": sid, "error": str(e)[:300]}})
            self.event(EV_FAILED, {**base, "error": note})
            log.error("resume_bridge.failed", extra={"fields": {**base, "error": note}})
            return False
        current = row.get("factory_session_id")
        new_id, must_record = restart_info(result, current)
        if must_record and new_id:  # Protocol B: record the new Factory session under the same id
            self._report_new_session(row, new_id)
        self.stats["delivered"] += 1
        payload = {**base, "mode": "restart" if new_id else "native", "factory_session_id": new_id or current,
                   "previous_factory_session_id": current if new_id else None,
                   "turn_status": _attr(result, "status"), "turn_ok": _attr(result, "ok")}
        self.event(EV_DELIVERED, payload)
        log.info("resume_bridge.delivered", extra={"fields": payload})
        return True

    def _report_new_session(self, row: dict, new_id: str) -> None:
        sid, role = _i(row.get("id")), row.get("role")
        step = f"restarted in Factory session {new_id} (resume bridge, Protocol B)"
        try:  # status stays resuming; if the droid already moved on, its client owns the row
            self.writer.call_reducer("upsert_agent_session", sid, role, new_id, "resuming", step, "", 0)
        except Exception as e:  # noqa: BLE001
            log.error("resume_bridge.report_session_failed", extra={"fields": {
                "session_id": sid, "factory_session_id": new_id, "error": str(e)[:300]}})

    @staticmethod
    def _matched(res: Evaluation) -> list[dict]:
        out = []
        for leaf, rows in res.matched:
            keep = {"id", leaf.field, *(k for k, _ in leaf.filters)} - {None}
            out.append({"leaf": leaf.describe(),
                        "rows": [{k: r.get(k) for k in keep if k in r} for r in rows[:5]], "count": len(rows)})
        return out

    def message(self, w: Waiting, res: Evaluation, changed: str, elapsed_s: float | None) -> str:
        lines = [
            "The condition you were waiting for is now true. Continue your task.",
            f"Waited for: {w.pred.describe() if w.pred else w.raw}",
            f"What changed: {changed}",
        ]
        for m in self._matched(res):
            rows = "; ".join(", ".join(f"{k}={_short(v, 60)}" for k, v in sorted(r.items())) for r in m["rows"])
            lines.append(f"Now true: {m['leaf']} -> {m['count']} row(s): {rows}")
        lines.append(f"agent_session {w.session_id} (ticket {_i(w.row.get('ticket_id'))}), waited {elapsed_s}s.")
        payload = w.row.get("resume_payload_json")
        if payload:
            lines.append(f"Your saved resume_payload: {payload[:2000]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ poll + recovery
    def resync(self, why: str = "poll") -> None:
        """Rebuild the waiting set from the mirror and evaluate everything (safety net)."""
        rows = self._bus_rows("agent_session") if self.on_bus("agent_session") else []
        live = {_i(r.get("id")): r for r in rows if r.get("status") == "waiting_for"}
        with self._lock:
            for sid in [s for s in self.waiting if s not in live]:
                self._drop(sid)
        for sid, r in live.items():
            self.session_changed(r, Change("agent_session", why, r, None, None))
        self.recover_orphans(rows)

    def recover_orphans(self, sessions: list[dict] | None = None) -> int:
        if not self.on_bus("event_log") or self.orphan_s <= 0:
            return 0
        sessions = sessions if sessions is not None else self._bus_rows("agent_session")
        cutoff = _now_us() - int(self.orphan_s * 1e6)
        stale = {_i(r["id"]): r for r in sessions if r.get("status") == "resuming" and _i(r.get("last_update")) < cutoff}
        if not stale:
            return 0
        last_resumed: dict[int, dict] = {}
        closed: dict[int, int] = {}
        for e in self._bus_rows("event_log"):
            t = e.get("type")
            if t not in ("agent.resumed", EV_DELIVERING, EV_DELIVERED, EV_FAILED):
                continue
            p = _json(e.get("payload_json")) or {}
            sid = _i(p.get("session_id"))
            if sid not in stale:
                continue
            eid = _i(e.get("id"))
            if t == "agent.resumed":
                if eid > _i((last_resumed.get(sid) or {}).get("id")):
                    last_resumed[sid] = {**e, "_p": p}
            else:
                closed[sid] = max(closed.get(sid, 0), eid)
        n = 0
        for sid, ev in last_resumed.items():
            eid = _i(ev["id"])
            if closed.get(sid, 0) > eid or eid in self.recovered or self.resumer() is None:
                continue
            p = ev["_p"]
            if p.get("mode") == "restart":
                continue  # the claimant already started a new session: nothing to deliver
            self.recovered.add(eid)
            row = stale[sid]
            pred = p.get("satisfied_predicate")
            try:
                desc = parse_predicate(pred).describe() if pred else "?"
            except PredicateError:
                desc = json.dumps(pred)
            text = "\n".join([
                "The condition you were waiting for is now true. Continue your task.",
                f"Waited for: {desc}",
                f"(Delivered late: the bridge that claimed this wake-up stopped before messaging you. "
                f"Claim reason: {p.get('reason', '')[:300]})",
            ] + ([f"Your saved resume_payload: {row['resume_payload_json'][:2000]}"] if row.get("resume_payload_json") else []))
            log.warning("resume_bridge.orphan_recovery", extra={"fields": {"session_id": sid, "agent_resumed_event": eid}})
            self.stats["recovered"] += 1
            self.pool.submit(self.deliver, row, text, predicate=pred, predicate_text=desc, recovered=True,
                             changed="recovered orphaned claim", agent_resumed_event=eid)
            n += 1
        return n

    def start_poller(self) -> None:
        if self._poller is not None or self.poll_s <= 0:
            return

        def loop() -> None:
            while not self._stop.wait(self.poll_s):
                try:
                    self.resync("poll")
                except Exception:  # noqa: BLE001
                    log.exception("resume_bridge.poll_error")

        self._poller = threading.Thread(target=loop, name="resume-bridge-poll", daemon=True)
        self._poller.start()

    def stop(self) -> None:
        self._stop.set()
        self.side.stop()

    def health(self) -> dict:
        with self._lock:
            return {"bridge": self.bridge_id, "waiting": {s: w.pred.describe() if w.pred else w.error
                                                          for s, w in self.waiting.items()},
                    "side_tables": sorted(self.side.tables), "stats": dict(self.stats)}


# =========================================================================== per-bus singleton

_instances: dict[int, ResumeBridge] = {}
_instances_lock = threading.Lock()


def get_bridge(bus: Any) -> ResumeBridge:
    with _instances_lock:
        b = _instances.get(id(bus))
        if b is None or b.bus is not bus:
            b = _instances[id(bus)] = ResumeBridge(bus)
            b.start_poller()
        return b
