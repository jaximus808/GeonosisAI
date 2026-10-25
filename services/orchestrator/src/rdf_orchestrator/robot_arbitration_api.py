"""TDM-114 HTTP surface for robot arbitration (logic in ``robot_arbitration.py``).

  GET  /robots/{robot_id}/queue               who holds the robot now and what's queued next (public read)
  POST /tickets/{ticket_id}/robot/claim_now   {by?}  run this ticket's baseline/eval now: a paused or
                                              running collection rollout of another ticket yields
                                              (``robot.reassigned``) and that ticket is held. 409 when a
                                              person is driving or another ticket's eval runs.
  POST /tickets/{ticket_id}/robot/resume      {by?}  end the hold on a ticket that gave its robot away
  POST /robots/{robot_id}/estop/clear         {by?}  SIM robots only: unlatch the soft e-stop from the web

The POSTs need ``X-RDF-Operator-Key`` = RDF_OPERATOR_KEY (the web proxy adds it after operator login).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from . import robot_arbitration as arb
from .interventions_api import _check_key

robot_arbitration_router = APIRouter(tags=["robot-arbitration"])


class ByBody(BaseModel):
    by: str = "operator"


def _by(body: ByBody | None) -> str:
    raw = (body.by if body else "") or "operator"
    return "".join(ch for ch in raw.lower() if ch.isalnum() or ch in "-_:@.")[:60] or "operator"


def _bus(request: Request):
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        raise HTTPException(503, "bus not started")
    return bus


async def _run(fn, *a, **kw):
    try:
        return await asyncio.to_thread(fn, *a, **kw)
    except arb.ArbitrationError as e:
        raise HTTPException(e.status, e.to_dict()) from e


@robot_arbitration_router.get("/robots/{robot_id}/queue")
def robot_queue(robot_id: int, request: Request) -> dict:
    return arb.robot_queue(_bus(request), robot_id)


@robot_arbitration_router.post("/tickets/{ticket_id}/robot/claim_now")
async def claim_now(ticket_id: int, request: Request, body: ByBody | None = None,
                    x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    _check_key(x_rdf_operator_key)
    return await _run(arb.get_arbiter(_bus(request)).claim_now, ticket_id, by=_by(body))


@robot_arbitration_router.post("/tickets/{ticket_id}/robot/resume")
async def resume(ticket_id: int, request: Request, body: ByBody | None = None,
                 x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    _check_key(x_rdf_operator_key)
    return await _run(arb.get_arbiter(_bus(request)).resume, ticket_id, by=_by(body))


@robot_arbitration_router.post("/robots/{robot_id}/estop/clear")
async def clear_estop(robot_id: int, request: Request, body: ByBody | None = None,
                      x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    _check_key(x_rdf_operator_key)
    return await _run(arb.get_arbiter(_bus(request)).clear_sim_estop, robot_id, by=_by(body))
