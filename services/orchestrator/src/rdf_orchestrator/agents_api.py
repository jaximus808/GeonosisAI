"""TDM-35 ``GET /agents``: every agent's status plus its last N normalised events (AgentFeed v1).

* Live: reads ``agent_session`` and ``event_log`` over SQL and runs ``agents_feed.build_feed``.
* ``?replay=1``: the recorded droid run (``fixtures/tdm35_replay/*.events.jsonl``, or
  ``RDF_AGENTS_REPLAY_DIR``), every agent and event flagged ``replay: true`` so the panel can
  label it. Use it when Factory is down during judging; it never touches SpacetimeDB.
* ``?replay=auto``: live, but falls back to the replay when SpacetimeDB is unreachable.
* ``GET /agents/replay/source``: the raw recorded rows, for the browser's timed playback.

Read-only: never calls a reducer. The browser can build the same feed from its subscriptions
(``apps/web/src/lib/agentfeed``); this route is for the MCP connector, scripts and replay.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query

from . import agents_feed

agents_router = APIRouter(tags=["agents"])

# event_log grows; the panel only needs recent history. Rows older than this many ids are skipped.
EVENT_WINDOW = int(os.environ.get("RDF_AGENTS_EVENT_WINDOW", "5000"))


class Tdm35Stdb(Protocol):
    async def sql(self, query: str) -> list[dict]: ...


_tdm35_client: Any = None


async def get_agents_stdb() -> Tdm35Stdb:
    """FastAPI dependency: one shared keep-alive client (tests override this)."""
    global _tdm35_client
    if _tdm35_client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import AsyncSpacetimeClient

        _tdm35_client = AsyncSpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"))
    return _tdm35_client


def _ts(v: Any) -> int:
    from rdf_common.spacetime_client import ts_micros

    try:
        return ts_micros(v)
    except (TypeError, ValueError, KeyError, IndexError):
        return 0


def _opt(v: Any) -> Any:
    """SATS option as decoded by rdf_common ({"some": x} / {"none": []} / x / None) -> x | None."""
    if isinstance(v, dict):
        if "some" in v:
            return v["some"]
        if "none" in v:
            return None
    if isinstance(v, list) and len(v) == 2 and v[0] in (0, 1):  # raw [tag, value]
        return v[1] if v[0] == 0 else None
    return v


async def load_live(sql) -> tuple[list[dict], list[dict]]:
    """(agent_session rows, event_log rows) in the plain shape ``build_feed`` takes."""
    sessions = []
    for r in await sql("SELECT * FROM agent_session"):
        sessions.append({
            "id": int(r["id"]), "ticket_id": int(r.get("ticket_id") or 0), "role": r.get("role") or "",
            "name": r.get("name") or "", "factory_session_id": _opt(r.get("factory_session_id")),
            "factory_session_ids_json": r.get("factory_session_ids_json") or "[]",
            "status": r.get("status") or "", "current_step": r.get("current_step") or "",
            "waiting_for_json": _opt(r.get("waiting_for_json")),
            "last_update_us": _ts(r.get("last_update")), "created_at_us": _ts(r.get("created_at")),
        })
    q = "SELECT * FROM event_log"
    if EVENT_WINDOW > 0:
        top = await sql("SELECT id FROM event_log")  # SpacetimeDB SQL has no ORDER BY/LIMIT
        hi = max((int(r["id"]) for r in top), default=0)
        if hi > EVENT_WINDOW:
            q += f" WHERE id > {hi - EVENT_WINDOW}"
    events = [{"id": int(r["id"]), "ts_us": _ts(r.get("ts")), "actor": r.get("actor") or "",
               "type": r.get("type") or "", "payload_json": r.get("payload_json") or ""}
              for r in await sql(q)]
    return sessions, events


def replay_dir() -> Path:
    env = os.environ.get("RDF_AGENTS_REPLAY_DIR")
    return Path(env) if env else agents_feed.REPLAY_DIR


@agents_router.get("/agents")
async def get_agents(ticket_id: int | None = None, limit: int = Query(agents_feed.DEFAULT_LIMIT, ge=0, le=500),
                     replay: str = "0", stdb: Tdm35Stdb = Depends(get_agents_stdb)) -> dict:
    """AgentFeed v1: ``{v, replay, generated_at_us, agents: [...]}``; see services/factory/README.md."""
    mode = replay.lower()
    if mode in ("1", "true", "yes"):
        return _replay(limit)
    try:
        sessions, events = await load_live(stdb.sql)
    except Exception as e:  # noqa: BLE001 - surface the outage (or fall back when asked)
        if mode == "auto":
            feed = _replay(limit)
            feed["replay_reason"] = f"SpacetimeDB unreachable: {type(e).__name__}"
            return feed
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}: {e}") from None
    return agents_feed.build_feed(sessions, events, ticket_id=ticket_id, limit=limit)


def _replay(limit: int) -> dict:
    try:
        return agents_feed.replay_feed(replay_dir(), limit=limit)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from None


@agents_router.get("/agents/replay/source")
def get_agents_replay_source() -> dict:
    """The raw recording behind ``?replay=1``: ``{v, replay: true, sessions, events, replay_source}``.

    ``events`` are event rows (``{id: "rec:<asid>:<line>", ts_us, actor, type, payload}``) that
    ``buildAgentFeed`` (apps/web/src/lib/agentfeed) normalises itself, so the browser can play the
    recording back over time with the same code as live mode.
    """
    d = replay_dir()
    paths = sorted(d.glob("*.events.jsonl")) if d.is_dir() else []
    if not paths:
        raise HTTPException(404, f"no *.events.jsonl recordings in {d}")
    sessions, events = agents_feed.load_recorded_events(paths)
    return {"v": agents_feed.FEED_VERSION, "replay": True, "sessions": sessions, "events": events,
            "replay_source": [p.name for p in paths]}


# TDM-31: POST /agents/{session_id}/wait lives in agents_wait_api; mounted here so app.py needs no change.
from .agents_wait_api import agents_wait_router  # noqa: E402

agents_router.include_router(agents_wait_router)
