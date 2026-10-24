"""TDM-71 report routes: the ticket's deliverable, built only from SpacetimeDB rows (``report.py``).

    GET  /tickets/{id}/report            JSON report (``?include_markdown=true`` adds ``markdown``)
    GET  /tickets/{id}/report.md         the same report rendered as markdown (text/markdown)
    POST /tickets/{id}/report/save       render + store ``.md`` and ``.json`` in the media store
                                         (key ``reports/<db>/ticket-<id>/report-e<last event>-<hash12>``)
                                         and append a ``report.saved`` event; returns the URIs

Read by the dashboard report page (TDM-79, ``apps/web`` ``/tickets/[id]/report``) and the MCP
``get_report`` tool (TDM-80). The GETs never call a reducer. A report built from ``[FAKE]`` harness
rows says so in ``fake.banner`` (JSON) and in a warning at the top of the markdown.
Mounted from ``app.py``.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from . import report
from .log import get_logger

log = get_logger("report_api")

report_router = APIRouter(tags=["tickets"])


class Tdm71Stdb(Protocol):
    async def sql(self, query: str) -> list[dict]: ...
    async def call_reducer(self, name: str, /, *args: Any) -> None: ...


_tdm71_client: Any = None


async def get_report_stdb() -> Tdm71Stdb:
    """FastAPI dependency: one shared keep-alive client (tests override this)."""
    global _tdm71_client
    if _tdm71_client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import AsyncSpacetimeClient

        _tdm71_client = AsyncSpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"))
    return _tdm71_client


def get_report_storage() -> Any:
    """FastAPI dependency: the media store backend (TDM-4). Tests override this."""
    from .storage import get_backend

    return get_backend()


async def _build(stdb: Tdm71Stdb, ticket_id: int) -> dict:
    try:
        tables = await report.load_tables(stdb.sql, ticket_id)
    except Exception as e:  # noqa: BLE001 - surface the outage, never a partial report
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}: {e}") from None
    try:
        return report.assemble(tables, ticket_id)
    except LookupError:
        raise HTTPException(404, f"No ticket {ticket_id}.") from None


@report_router.get("/tickets/{ticket_id}/report")
async def get_report(ticket_id: int, include_markdown: bool = False,
                     stdb: Tdm71Stdb = Depends(get_report_stdb)) -> dict:
    """What was bought, why, what it cost, success before and after, the checkpoint, the log seal."""
    r = await _build(stdb, ticket_id)
    if include_markdown:
        r["markdown"] = report.render_markdown(r)
    return r


@report_router.get("/tickets/{ticket_id}/report.md", response_class=PlainTextResponse)
async def get_report_markdown(ticket_id: int, stdb: Tdm71Stdb = Depends(get_report_stdb)) -> PlainTextResponse:
    r = await _build(stdb, ticket_id)
    return PlainTextResponse(report.render_markdown(r), media_type="text/markdown; charset=utf-8")


def _db_name(stdb: Any) -> str:
    db = getattr(getattr(stdb, "config", None), "db", None) or os.environ.get("SPACETIME_DB_NAME") or "unknown-db"
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(db)) or "unknown-db"


@report_router.post("/tickets/{ticket_id}/report/save")
async def save_report(ticket_id: int, stdb: Tdm71Stdb = Depends(get_report_stdb),
                      store: Any = Depends(get_report_storage)) -> dict:
    """Store the markdown and JSON in the media store and log ``report.saved`` (idempotent per
    event-log state: the key carries the last event id and the seal hash)."""
    r = await _build(stdb, ticket_id)
    md = report.render_markdown(r)
    base = (f"reports/{_db_name(stdb)}/ticket-{ticket_id}/"
            f"report-e{r['event_log']['last_id'] or 0}-{r['event_log_hash'][:12]}")
    try:
        md_uri = await asyncio.to_thread(store.put, base + ".md", md.encode("utf-8"), "text/markdown")
        json_uri = await asyncio.to_thread(store.put, base + ".json",
                                           json.dumps(r, indent=2, sort_keys=True).encode("utf-8"), "application/json")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"media store write failed: {type(e).__name__}: {e}") from None
    payload = {"ticket_id": ticket_id, "uri": md_uri, "json_uri": json_uri, "event_log_hash": r["event_log_hash"],
               "event_log_last_id": r["event_log"]["last_id"], "fake": r["fake"]["any"],
               "report_version": r["report_version"]}
    try:
        await stdb.call_reducer("append_event", "orchestrator:report", "report.saved", json.dumps(payload))
    except Exception as e:  # noqa: BLE001 - the files are stored; say the event is missing
        log.warning("report.saved_event_failed", extra={"fields": {"ticket_id": ticket_id, "error": str(e)}})
        payload["event_error"] = f"{type(e).__name__}: {e}"
    log.info("report.saved", extra={"fields": {"ticket_id": ticket_id, "uri": md_uri, "fake": r["fake"]["any"]}})
    return {**payload, "missing": r["missing"], "fake_banner": r["fake"]["banner"]}
