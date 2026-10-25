"""[SIM] Run a shipped ticket's deployed policy on its sim robot, for the owner to watch.

    POST /tickets/{ticket_id}/deployment/run      header X-RDF-Operator-Key (the web server adds it after operator login)
      -> 200 {ok, command_id, ticket_id, robot_id, robot_name, checkpoint_uri, runs_on: "sim"}

Issues ``run_baseline`` {purpose: "deployment_run", checkpoint_uri: ticket.deployed_checkpoint_uri} on the
ticket's robot. The robot client (``eval_trials.py``) runs ONE rollout of that checkpoint on the MuJoCo sim:
home, scene reset, the policy episode with the sim success check. No eval_run, no eval_trial row, no
recording, so the ticket's numbers never change. Logs ``deployment.run_started`` in event_log; the robot
logs ``deployment.run_finished``.

Refuses (never sends a policy command to them): a real robot or a teleop-only one (``robot_is_sim`` /
``robot_can_eval``), a ticket that is not shipped or has no deployed checkpoint, and a robot that is
offline, in estop, running a job, or wanted by another ticket's baseline/eval (``robot_busy``).
Only existing reducers: ``issue_robot_command`` and ``append_event``.
"""
from __future__ import annotations

import hmac
import json
from typing import Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException

from .log import get_logger
from .robot_commands_api import get_operator_key, get_robot_commands_stdb
from .robot_mode import robot_can_eval, robot_is_sim

log = get_logger("deployment_api")
deployment_router = APIRouter(tags=["deployment"])

PURPOSE = "deployment_run"
ACTOR = "orchestrator:deployment"
OPEN = ("issued", "acked")
JOBS = ("run_baseline", "run_eval", "start_teleop", "reset_request", "home", "load_checkpoint")
EVAL_STAGES = ("baseline", "eval")
TIMEOUT_S = 90.0


class Stdb(Protocol):
    async def sql(self, query: str) -> list[dict]: ...
    async def call_reducer(self, name: str, /, *args: Any) -> None: ...


def _i(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message, **extra})


def _opt(v: Any) -> Any:
    """SpacetimeDB option columns come back as the value, None, or {"some": v} / {"none": ...}."""
    if isinstance(v, dict):
        return v.get("some")
    if isinstance(v, list) and len(v) == 2 and v[0] in (0, 1):  # [tag, value] SATS encoding
        return v[1] if v[0] == 0 else None
    return v


async def _one(stdb: Stdb, table: str, row_id: int) -> dict | None:
    rows = await stdb.sql(f"SELECT * FROM {table} WHERE id = {int(row_id)}")
    return rows[0] if rows else None


@deployment_router.post("/tickets/{ticket_id}/deployment/run")
async def run_deployment(ticket_id: int,
                         x_rdf_operator_key: str | None = Header(default=None),
                         x_human_id: str = Header(default="operator", pattern=r"^[A-Za-z0-9._@:+-]{1,64}$"),
                         stdb: Stdb = Depends(get_robot_commands_stdb),
                         operator_key: str | None = Depends(get_operator_key)) -> dict[str, Any]:
    if not operator_key:
        raise _err(503, "operator_key_unset", "Deployment runs are off: set RDF_OPERATOR_KEY for the orchestrator and the web server.")
    if not x_rdf_operator_key or not hmac.compare_digest(x_rdf_operator_key.encode(), operator_key.encode()):
        raise _err(401, "operator_login_required", "Operator login required to run a deployed policy.")

    ticket = await _one(stdb, "ticket", ticket_id)
    if ticket is None:
        raise _err(404, "ticket_not_found", f"No ticket {ticket_id}.")
    uri = str(_opt(ticket.get("deployed_checkpoint_uri")) or "")
    if ticket.get("status") != "shipped" or not uri:
        raise _err(409, "not_shipped", f"Ticket {ticket_id} is {ticket.get('status')}; only a shipped ticket with a "
                                        "deployed checkpoint can run its policy.")

    robot_id = _i(ticket.get("robot_id"))
    robot = await _one(stdb, "robot", robot_id) if robot_id else None
    if robot is None:
        raise _err(404, "robot_not_found", f"Ticket {ticket_id} has no robot row (robot_id {robot_id}).")
    if not robot_is_sim(robot) or not robot_can_eval(robot):
        raise _err(403, "robot_not_sim", f"{robot.get('name')} is a real robot. Deployment runs go to the sim robot "
                                          "only; nothing was sent.", robot_id=robot_id)
    name = robot.get("name")
    if robot.get("status") in ("offline", "estop", None, ""):
        raise _err(409, "robot_offline", f"{name} is {robot.get('status') or 'offline'}. Start the sim robot and try again.",
                   robot_id=robot_id)
    cmds = await stdb.sql(f"SELECT * FROM robot_command WHERE robot_id = {robot_id}")
    jobs = sorted((c for c in cmds if c.get("status") in OPEN and c.get("type") in JOBS), key=lambda c: _i(c.get("id")))
    if jobs:
        j = jobs[0]
        mine = _i(j.get("ticket_id")) == ticket_id and PURPOSE in str(j.get("payload_json") or "")
        raise _err(409, "robot_busy", f"Robot busy: {name} is running {j.get('type')} #{j.get('id')}"
                                       + (" (your policy is already running)." if mine else ". Try again when it is idle."),
                   robot_id=robot_id, command_id=_i(j.get("id")), already_running=mine)
    if robot.get("status") in ("busy", "paused"):
        raise _err(409, "robot_busy", f"Robot busy: {name} is {robot.get('status')}. Try again when it is idle.",
                   robot_id=robot_id)
    tickets = await stdb.sql("SELECT * FROM ticket")
    waiting = [t for t in tickets if _i(t.get("robot_id")) == robot_id and _i(t.get("id")) != ticket_id
               and t.get("status") in EVAL_STAGES]
    if waiting:
        t = waiting[0]
        raise _err(409, "robot_busy", f"Robot busy: TKT-{_i(t.get('id')):04d} is in {t.get('status')} on {name}. "
                                       "Its eval goes first.", robot_id=robot_id, ticket_id=_i(t.get("id")))

    payload = {"purpose": PURPOSE, "checkpoint_uri": uri, "trials": 1, "timeout_s": TIMEOUT_S, "kind": "deployment",
               "robot_mode": "sim", "by": f"human:{x_human_id}",
               "note": "[SIM] one watched rollout of the deployed checkpoint: not recorded, not an eval trial"}
    try:
        await stdb.call_reducer("issue_robot_command", robot_id, "run_baseline", json.dumps(payload), ticket_id)
    except Exception as e:  # noqa: BLE001
        status, msg = getattr(e, "status", None), str(getattr(e, "message", "") or e)[:400]
        if isinstance(status, int) and (400 <= status < 500 or status == 530):
            code = "robot_busy" if "busy" in msg.lower() else "command_refused"
            raise _err(409, code, ("Robot busy: " if code == "robot_busy" else "") + msg, robot_id=robot_id) from e
        raise _err(502, "stdb_unreachable", f"SpacetimeDB unreachable: {msg}") from e

    cmd_id = None
    try:
        rows = await stdb.sql(f"SELECT * FROM robot_command WHERE robot_id = {robot_id}")
        runs = [r for r in rows if r.get("type") == "run_baseline" and PURPOSE in str(r.get("payload_json") or "")]
        if runs:
            cmd_id = _i(max(runs, key=lambda r: _i(r.get("id")))["id"])
    except Exception as e:  # noqa: BLE001 - the command went out; the id is a nicety
        log.warning("deployment.readback_failed", extra={"fields": {"error": str(e)[:200]}})

    event = {"ticket_id": ticket_id, "robot_id": robot_id, "robot_name": name, "command_id": cmd_id,
             "checkpoint_uri": uri, "runs_on": "sim", "by": f"human:{x_human_id}"}
    try:
        await stdb.call_reducer("append_event", ACTOR, "deployment.run_started", json.dumps(event))
    except Exception as e:  # noqa: BLE001 - evidence is best effort; the run is already going
        log.warning("deployment.event_failed", extra={"fields": {"error": str(e)[:200]}})
    log.info("deployment.run_started", extra={"fields": event})
    return {"ok": True, **event}


__all__ = ["deployment_router", "run_deployment"]
