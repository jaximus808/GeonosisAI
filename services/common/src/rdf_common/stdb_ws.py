"""Drop-in replacement for the TDM-7 spike client ``spikes/tdm-7-spacetime/stdb_ws.py`` (TDM-16).

Same names and the same raw-row semantics as the spike, built on ``rdf_common.spacetime_client``,
so code written against the spike moves over by changing one import line::

    from rdf_common.stdb_ws import StdbSubscriber, call_reducer_http, sql, ts_micros

Rows here are the server's raw JSON values (a timestamp is ``{"__timestamp_micros...": n}``,
an option is ``[tag, value]``); only positional rows are mapped to dicts. New code should use
``rdf_common.spacetime_client`` instead: decoded rows, ``on_update``, a row cache, typed reducer
args, and a keep-alive HTTP client.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Iterable

import httpx

from rdf_common.spacetime_client import (
    PROTOCOL,
    Row,
    SpacetimeConfig,
    SpacetimeError,
    Subscription,
    fetch_schema,
    ts_micros,
)

RowCb = Callable[[str, Row, dict], None]

__all__ = ["PROTOCOL", "StdbSubscriber", "call_reducer_http", "sql", "table_columns", "ts_micros"]

_http: httpx.Client | None = None


def _client() -> httpx.Client:
    global _http
    if _http is None:  # one keep-alive connection per process
        _http = httpx.Client(transport=httpx.HTTPTransport(retries=2))
    return _http


def call_reducer_http(reducer: str, args: list | dict, *, host=None, db=None, token=None, timeout=10.0) -> None:
    """POST /call/<reducer> with raw JSON args. Raises SpacetimeError on a non-2xx answer."""
    cfg = SpacetimeConfig.resolve(host, db, token)
    r = _client().post(f"{cfg.base}/call/{reducer}", json=args, headers=cfg.headers(), timeout=timeout)
    if r.status_code >= 300:
        raise SpacetimeError(r.status_code, r.text.strip(), reducer=reducer)


def sql(query: str, *, host=None, db=None, token=None, timeout=10.0) -> list[Row]:
    """POST /sql. Rows of the last statement as dicts of raw values."""
    cfg = SpacetimeConfig.resolve(host, db, token)
    r = _client().post(f"{cfg.base}/sql", content=query.encode(), headers=cfg.headers(), timeout=timeout)
    if r.status_code >= 300:
        raise SpacetimeError(r.status_code, r.text.strip())
    res = r.json()[-1]
    cols = [e["name"]["some"] if isinstance(e["name"], dict) else e["name"] for e in res["schema"]["elements"]]
    return [dict(zip(cols, row)) for row in res["rows"]]


def table_columns(*, host=None, db=None, timeout=10.0) -> dict[str, list[str]]:
    """GET /schema -> {table_name: [column names in declaration order]}."""
    s = fetch_schema(host, db, timeout=timeout)
    return {t: s.columns(t) for t in s.tables}


class StdbSubscriber(Subscription):
    """The spike's subscriber API (raw rows, no on_update) on the shared implementation."""

    def __init__(
        self,
        queries: Iterable[str],
        *,
        on_insert: RowCb | None = None,
        on_delete: RowCb | None = None,
        on_applied: Callable[[dict[str, list[Row]]], None] | None = None,
        on_connect: Callable[[str, str], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        host: str | None = None,
        db: str | None = None,
        token: str | None = None,
        min_backoff: float = 1.0,
        max_backoff: float = 30.0,
    ):
        super().__init__(
            queries, on_insert=on_insert, on_delete=on_delete, on_applied=on_applied,
            on_connect=on_connect, on_error=on_error, decode=False,
            host=host, db=db, token=token, min_backoff=min_backoff, max_backoff=max_backoff,
        )
        self._connected = asyncio.Event()

    @property
    def connected(self) -> asyncio.Event:
        return self._connected

    @property
    def columns(self) -> dict[str, list[str]]:
        return {t: self.schema.columns(t) for t in self.schema.tables} if self.schema else {}

    async def call_reducer(self, reducer: str, args: list | dict) -> int:  # type: ignore[override]
        """Raw args over the open socket (no schema encoding), like the spike."""
        await self._connected.wait()
        rid = self._next_id()
        await self._send({"CallReducer": {"reducer": reducer, "args": json.dumps(args), "request_id": rid, "flags": 0}})
        return rid

    def _err(self, e: Exception) -> None:
        if isinstance(e, SpacetimeError) and e.status == 530:  # spike wording: "reducer failed: ..."
            e = RuntimeError(f"reducer failed: {e.message}")
        super()._err(e)
