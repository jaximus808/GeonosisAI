"""TDM-31 ``POST /agents/{session_id}/wait``: park an agent_session on a predicate over HTTP.

Body: ``{"predicate": <text or JSON>, "resume_payload": <any JSON, optional>, "timeout_s": <s, optional>}``

* ``predicate`` is the same thing a droid prints after ``WAITING_FOR``: the text form
  ``"dataset:accepted_count>=1"`` (``timeout=30m`` allowed) or a JSON object (all/any, agg, where,
  ``timeout_s``), see services/factory/README.md "Wait-for protocol". It is parsed with
  ``rdf_factory.events.parse_waiting_for`` / ``scope_to_ticket`` (so every leaf is scoped to the session's
  ticket) and validated with the resume bridge's own ``parse_predicate`` before anything is written, so a
  predicate the bridge cannot evaluate is a 422 here instead of a silent forever-wait.
* Then it calls ``agent_wait_for(session_id, predicate_json, resume_payload_json)`` (TDM-13). The TDM-27
  bridge picks the row up from its subscription like any other wait and resumes the session through
  ``FactoryClient.resume`` with the predicate, the payload and a ticket snapshot.

Who calls it: orchestrator code, scripts, the MCP connector, or an operator parking a droid whose turn
ended ``paused`` (no final line). A Factory droid parks itself by ending its turn with the
``WAITING_FOR`` line instead: the TDM-30 client makes this same reducer call at turn end, and a turn that
ends without the line would set the row back to ``paused``. So a droid must not call this route mid-turn.

Errors: 404 unknown session, 409 session done/failed (or the reducer refused), 422 bad predicate, 502
SpacetimeDB unreachable.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import resume_bridge as rb

agents_wait_router = APIRouter(tags=["agents"])

TERMINAL = ("done", "failed")


class WaitBody(BaseModel):
    predicate: str | dict[str, Any]
    resume_payload: Any = None
    timeout_s: float | None = Field(default=None, ge=0, description="0 = never time out; omitted = bridge default")


async def get_wait_stdb():
    """FastAPI dependency (tests override it): needs ``sql`` and ``call_reducer``. Shares TDM-35's client."""
    from .agents_api import get_agents_stdb  # lazy: agents_api mounts this router at import time

    return await get_agents_stdb()


def build_predicate(predicate: str | dict[str, Any], ticket_id: int, timeout_s: float | None) -> dict[str, Any]:
    """Text or JSON -> the stored predicate (ticket-scoped, with timeout_s). Raises PredicateError."""
    from rdf_factory.events import parse_waiting_for, scope_to_ticket

    if isinstance(predicate, str):
        line = predicate.strip().removeprefix("WAITING_FOR").strip()
        if not line:
            raise rb.PredicateError("predicate is empty")
        p = parse_waiting_for("WAITING_FOR " + line, ticket_id=ticket_id)
        if p is None:
            raise rb.PredicateError("predicate could not be parsed")
    else:
        p = scope_to_ticket(json.loads(json.dumps(predicate)), ticket_id)
    if timeout_s is not None:
        p["timeout_s"] = timeout_s
    rb.parse_predicate(p)  # the bridge's own validation: refuse what it could never evaluate
    return p


def payload_json(payload: Any) -> str:
    if payload is None or payload == "":
        return ""
    if isinstance(payload, str):
        try:
            json.loads(payload)
            return payload
        except ValueError:
            return json.dumps({"note": payload})
    return json.dumps(payload, default=str)


@agents_wait_router.post("/agents/{session_id}/wait")
async def post_agent_wait(session_id: int, body: WaitBody, stdb: Any = Depends(get_wait_stdb)) -> dict:
    try:
        rows = await stdb.sql(f"SELECT * FROM agent_session WHERE id = {int(session_id)}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB unreachable: {e}") from None
    if not rows:
        raise HTTPException(404, f"unknown agent_session {session_id}")
    row = rows[0]
    if row.get("status") in TERMINAL:
        raise HTTPException(409, f"agent_session {session_id} is {row.get('status')}")
    ticket_id = int(row.get("ticket_id") or 0)
    try:
        pred = build_predicate(body.predicate, ticket_id, body.timeout_s)
        node = rb.parse_predicate(pred)
    except (rb.PredicateError, ValueError, TypeError) as e:
        raise HTTPException(422, f"bad predicate: {e}") from None
    try:
        await stdb.call_reducer("agent_wait_for", int(session_id), json.dumps(pred), payload_json(body.resume_payload))
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status", None)
        raise HTTPException(409 if status == 530 else 502, f"agent_wait_for failed: {e}") from None
    return {"session_id": int(session_id), "ticket_id": ticket_id, "status": "waiting_for", "predicate": pred,
            "describe": node.describe(), "timeout_s": pred.get("timeout_s")}
