"""Flywheel HTTP surface (TDM-22).

    POST /tickets/{id}/advance   {"to"?: status, "reason"?: str, "by"?: str}
        Manual nudge for demos. Logged as a human: a ``flywheel.transition`` event with actor
        ``human:<by>`` and manual=true, and the ``set_ticket_status`` note says who and why.
        Without ``to`` it takes the machine's own decision if it has one, else the default
        forward edge (eval has none: name shipped|diagnose|plan_data|dispatch|failed).
        202 with the observed status once the row changed (or "pending" after 5 s).
    GET  /flywheel/tickets/{id}  what the machine sees: status, its decision and why, money, step.
    GET  /flywheel/steps         stage -> step, owner, wired or not.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["flywheel"])


class AdvanceBody(BaseModel):
    to: str | None = None
    reason: str = ""
    by: str = "human"


def _fw(request: Request):
    fw = getattr(request.app.state, "flywheel", None)
    if fw is None:
        from .handlers.flywheel import FLYWHEEL as fw  # the default registry's instance
    return fw


@router.post("/tickets/{ticket_id}/advance", status_code=202)
async def advance(ticket_id: int, request: Request, body: AdvanceBody | None = None) -> dict:
    body = body or AdvanceBody()
    bus, fw = request.app.state.bus, _fw(request)
    try:
        out = await fw.advance(bus, ticket_id, to=body.to, reason=body.reason, by=body.by or "human")
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    for _ in range(100):  # the reducer call is fire-and-forget; watch the mirror for the result
        t = bus.get("ticket", ticket_id)
        if t is not None and (t.get("status") != out["from"] or t.get("iteration") != out["iteration"]):
            return {**out, "observed_status": t.get("status"), "observed_iteration": t.get("iteration")}
        await asyncio.sleep(0.05)
    return {**out, "observed_status": "pending"}


@router.get("/flywheel/tickets/{ticket_id}")
def describe(ticket_id: int, request: Request) -> dict:
    d = _fw(request).describe(request.app.state.bus, ticket_id)
    if d is None:
        raise HTTPException(404, f"unknown ticket {ticket_id}")
    return d


@router.get("/flywheel/steps")
def steps(request: Request) -> list[dict]:
    return _fw(request).step_table(request.app.state.bus)
