"""TDM-69 HTTP surface for interventions (logic in ``interventions.py``).

  GET  /tickets/{ticket_id}/interventions   every intervention of the ticket (detector, task, claim
                                            wait, takeover length, episode, review, timeouts) plus
                                            totals, rebuilt from the ``intervention.*`` event trail
  POST /robots/{robot_id}/interventions/flag {note?, by?}   the robot page's "Failing" button:
                                            pause the running policy rollout now and route a paid
                                            takeover (same path as a robot-detected failure).
                                            Needs ``X-RDF-Operator-Key`` = RDF_OPERATOR_KEY (the same
                                            operator key as TDM-78's POST /robots/{id}/commands; the
                                            web server keeps it server side). Unset key -> 503.
"""
from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, SecretStr

from . import interventions as ivm

interventions_router = APIRouter(tags=["interventions"])


class FlagBody(BaseModel):
    note: str = ""
    by: str = "operator"


def get_operator_key() -> str | None:
    """RDF_OPERATOR_KEY from the environment or the repo .env (None = not configured)."""
    from rdf_common.settings import RdfSettings

    class _Tdm69Settings(RdfSettings):
        rdf_operator_key: SecretStr | None = None

    k = _Tdm69Settings().rdf_operator_key
    return k.get_secret_value() if k else None


def _check_key(given: str | None) -> None:
    expected = get_operator_key()
    if not expected:
        raise HTTPException(503, "operator actions are off: set RDF_OPERATOR_KEY for the orchestrator and the web server")
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "missing or wrong X-RDF-Operator-Key")


def _mgr(request: Request) -> ivm.InterventionManager:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        raise HTTPException(503, "bus not started")
    return ivm.get_manager(bus)


@interventions_router.get("/tickets/{ticket_id}/interventions")
def list_interventions(ticket_id: int, request: Request) -> dict:
    return _mgr(request).summary(ticket_id)


@interventions_router.post("/robots/{robot_id}/interventions/flag")
async def flag_failing(robot_id: int, body: FlagBody, request: Request,
                       x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    _check_key(x_rdf_operator_key)
    mgr = _mgr(request)
    note = (body.note or "operator pressed Failing").strip()[:300]
    by = "".join(ch for ch in (body.by or "operator").lower() if ch.isalnum() or ch in "-_:")[:60] or "operator"
    out = await asyncio.to_thread(mgr.start, robot_id, detector="operator", reason=note, signal_actor=f"web:{by}")
    live = mgr.live_on_robot(robot_id)
    return {"outcome": out, "intervention": live.to_dict() if live else None}
