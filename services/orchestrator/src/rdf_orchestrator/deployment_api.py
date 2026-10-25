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

TDM-126, the REAL arm: body ``{"real_arm": true, "confirm": "HANDS_ON_ESTOP", "checkpoint": "before"|"after",
"robot_id"?}`` -> ``runs_on: "real"``. Accepted only when ALL hold, else refused as above:
  * the confirm string is exact (``confirm_required``);
  * the robot is real and advertises ``policy: true`` (``robot_can_eval`` and the explicit flag; the Jetson's
    tdm119_teleop sets it only with TDM119_ALLOW_POLICY=1) -> ``robot_no_policy`` / ``no_real_robot``;
  * the ticket is shipped, and the checkpoint is resolved SERVER-SIDE from the choice, never from the browser:
    ``after`` = the ticket's ``deployed_checkpoint_uri``, ``before`` = the YAM-adapted pi0.5 smoke checkpoint
    (``REAL_YAM_BASELINE_CHECKPOINT``; base pi05_base is DROID-layout and never goes to the YAM). Both must be
    ``modal-vol://rdf-checkpoints/...`` (no ``fake://``, no stub) -> ``checkpoint_not_real``.
The robot is ``robot_id`` when given, else the ticket's robot if real, else its collect robot (TDM-125) if
real, else the only online policy-capable real robot. The payload carries the ticket's exact training prompt
(the training run's ``hyperparams.prompt`` for that checkpoint, else run_settings', else the task
description), ``timeout_s`` 25 (the robot caps at 30) and ``speed_scale`` 0.25. Logs
``deployment.real_run_started``; the robot logs ``deployment.real_run_finished``.
"""
from __future__ import annotations

import hmac
import json
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .log import get_logger
from .robot_commands_api import get_operator_key, get_robot_commands_stdb
from .robot_mode import (
    REAL_YAM_BASELINE_CHECKPOINT,
    collect_robot_id,
    hyperparams,
    real_checkpoint_ok,
    robot_can_eval,
    robot_config,
    robot_is_sim,
)

log = get_logger("deployment_api")
deployment_router = APIRouter(tags=["deployment"])

PURPOSE = "deployment_run"
ACTOR = "orchestrator:deployment"
OPEN = ("issued", "acked")
JOBS = ("run_baseline", "run_eval", "start_teleop", "reset_request", "home", "load_checkpoint")
EVAL_STAGES = ("baseline", "eval")
TIMEOUT_S = 90.0
REAL_PURPOSE = "deployment_real_run"
REAL_CONFIRM = "HANDS_ON_ESTOP"
REAL_TIMEOUT_S = 25.0
REAL_SPEED_SCALE = 0.25


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    real_arm: bool = False
    confirm: str | None = Field(default=None, max_length=64)
    checkpoint: Literal["before", "after"] = "after"
    robot_id: int | None = Field(default=None, ge=1)


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
                         body: RunBody | None = Body(default=None),
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

    if body is not None and body.real_arm:
        return await _run_real(ticket_id, ticket, uri, body, x_human_id, stdb)

    robot_id = _i(ticket.get("robot_id"))
    robot = await _one(stdb, "robot", robot_id) if robot_id else None
    if robot is None:
        raise _err(404, "robot_not_found", f"Ticket {ticket_id} has no robot row (robot_id {robot_id}).")
    if not robot_is_sim(robot) or not robot_can_eval(robot):
        raise _err(403, "robot_not_sim", f"{robot.get('name')} is a real robot. Deployment runs go to the sim robot "
                                          "only; nothing was sent.", robot_id=robot_id)
    await _ensure_free(stdb, robot, ticket_id, "Start the sim robot and try again.")
    payload = {"purpose": PURPOSE, "checkpoint_uri": uri, "trials": 1, "timeout_s": TIMEOUT_S, "kind": "deployment",
               "robot_mode": "sim", "by": f"human:{x_human_id}",
               "note": "[SIM] one watched rollout of the deployed checkpoint: not recorded, not an eval trial"}
    cmd_id = await _issue(stdb, robot_id, payload, ticket_id, PURPOSE)
    event = {"ticket_id": ticket_id, "robot_id": robot_id, "robot_name": robot.get("name"), "command_id": cmd_id,
             "checkpoint_uri": uri, "runs_on": "sim", "by": f"human:{x_human_id}"}
    await _event(stdb, "deployment.run_started", event)
    return {"ok": True, **event}


async def _ensure_free(stdb: Stdb, robot: dict, ticket_id: int, start_hint: str) -> None:
    """Refuse a robot that is offline / in estop, running a job, or wanted by another ticket's eval."""
    robot_id = _i(robot.get("id"))
    name = robot.get("name")
    if robot.get("status") in ("offline", "estop", None, ""):
        raise _err(409, "robot_offline", f"{name} is {robot.get('status') or 'offline'}. {start_hint}",
                   robot_id=robot_id)
    cmds = await stdb.sql(f"SELECT * FROM robot_command WHERE robot_id = {robot_id}")
    jobs = sorted((c for c in cmds if c.get("status") in OPEN and c.get("type") in JOBS), key=lambda c: _i(c.get("id")))
    if jobs:
        j = jobs[0]
        mine = _i(j.get("ticket_id")) == ticket_id and PURPOSE in str(j.get("payload_json") or "")  # also *_real_run
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


async def _issue(stdb: Stdb, robot_id: int, payload: dict, ticket_id: int, purpose: str) -> int | None:
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
        runs = [r for r in rows if r.get("type") == "run_baseline"
                and (_payload(r).get("purpose") == purpose)]
        if runs:
            cmd_id = _i(max(runs, key=lambda r: _i(r.get("id")))["id"])
    except Exception as e:  # noqa: BLE001 - the command went out; the id is a nicety
        log.warning("deployment.readback_failed", extra={"fields": {"error": str(e)[:200]}})
    return cmd_id


def _payload(row: dict) -> dict:
    try:
        p = json.loads(row.get("payload_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return p if isinstance(p, dict) else {}


async def _event(stdb: Stdb, type_: str, event: dict) -> None:
    try:
        await stdb.call_reducer("append_event", ACTOR, type_, json.dumps(event))
    except Exception as e:  # noqa: BLE001 - evidence is best effort; the run is already going
        log.warning("deployment.event_failed", extra={"fields": {"error": str(e)[:200]}})
    log.info(type_, extra={"fields": event})


# --------------------------------------------------------------------------- TDM-126: the real arm


def _real_policy_robot(robot: dict | None) -> bool:
    return robot is not None and not robot_is_sim(robot) and robot_can_eval(robot) \
        and robot_config(robot).get("policy") is True


async def _training_prompt(stdb: Stdb, ticket: dict, uri: str) -> str:
    """The exact prompt the fine-tune trained on (modal_client.build_manifest's rule)."""
    tid = _i(ticket.get("id"))
    runs = await stdb.sql(f"SELECT * FROM training_run WHERE ticket_id = {tid}")
    for r in sorted(runs, key=lambda r: -_i(r.get("id"))):
        if str(_opt(r.get("checkpoint_uri")) or "") == uri:
            p = str(hyperparams(r).get("prompt") or "").strip()
            if p:
                return p
            break
    rs = await stdb.sql(f"SELECT * FROM run_settings WHERE ticket_id = {tid}")
    p = str(hyperparams(rs[0] if rs else None).get("prompt") or "").strip()
    return p or str(ticket.get("task_description") or "").strip()


async def _pick_real_robot(stdb: Stdb, ticket: dict, robot_id: int | None) -> dict:
    if robot_id:
        r = await _one(stdb, "robot", robot_id)
        if r is None:
            raise _err(404, "robot_not_found", f"No robot {robot_id}.")
        tried = [r]
    else:
        tried = []
        own = await _one(stdb, "robot", _i(ticket.get("robot_id"))) if _i(ticket.get("robot_id")) else None
        rs = await stdb.sql(f"SELECT * FROM run_settings WHERE ticket_id = {_i(ticket.get('id'))}")
        cid = collect_robot_id(rs[0] if rs else None, ticket)
        coll = await _one(stdb, "robot", cid) if cid and cid != _i(ticket.get("robot_id")) else None
        tried = [x for x in (own, coll) if x is not None and not robot_is_sim(x)]
        if not tried:
            live = [x for x in await stdb.sql("SELECT * FROM robot")
                    if _real_policy_robot(x) and x.get("status") in ("online", "idle")
                    and not str(x.get("name") or "").startswith("test.")]
            if len(live) == 1:
                tried = live
        if not tried:
            raise _err(409, "no_real_robot", "No real robot for this ticket: its robot and collect robot are sim, and "
                                             "there is not exactly one online real arm with policy enabled. Nothing was sent.")
    for r in tried:
        if _real_policy_robot(r):
            return r
    r = tried[0]
    if robot_is_sim(r):
        raise _err(403, "robot_is_sim", f"{r.get('name')} is a sim robot; real_arm runs need a real arm.",
                   robot_id=_i(r.get("id")))
    raise _err(403, "robot_no_policy", f"{r.get('name')} does not advertise policy capability. On the Jetson set "
                                       "TDM119_ALLOW_POLICY=1 and restart geonosis-robot-teleop. Nothing was sent.",
               robot_id=_i(r.get("id")))


async def _run_real(ticket_id: int, ticket: dict, deployed: str, body: RunBody, who: str, stdb: Stdb) -> dict[str, Any]:
    if body.confirm != REAL_CONFIRM:
        raise _err(400, "confirm_required", f"A real-arm run needs confirm: {REAL_CONFIRM} (hands on the e-stop).")
    uri = REAL_YAM_BASELINE_CHECKPOINT if body.checkpoint == "before" else deployed
    if not real_checkpoint_ok(uri):
        raise _err(409, "checkpoint_not_real", f"The {body.checkpoint} checkpoint {uri!r} is not a real "
                                               "modal-vol://rdf-checkpoints/ checkpoint. Nothing goes to the real arm.",
                   checkpoint=body.checkpoint)
    robot = await _pick_real_robot(stdb, ticket, body.robot_id)
    robot_id, name = _i(robot.get("id")), robot.get("name")
    await _ensure_free(stdb, robot, ticket_id, "Start geonosis-robot-teleop on the Jetson and try again.")
    prompt = await _training_prompt(stdb, ticket, deployed)
    if not prompt:
        raise _err(409, "no_prompt", f"Ticket {ticket_id} has no training prompt.")
    payload = {"purpose": REAL_PURPOSE, "checkpoint_uri": uri, "checkpoint_choice": body.checkpoint, "prompt": prompt,
               "trials": 1, "timeout_s": REAL_TIMEOUT_S, "speed_scale": REAL_SPEED_SCALE, "real_arm": True,
               "confirm": REAL_CONFIRM, "kind": "deployment", "robot_mode": "real", "by": f"human:{who}",
               "note": "TDM-126: one supervised real-arm rollout, capped, not recorded, not an eval trial"}
    cmd_id = await _issue(stdb, robot_id, payload, ticket_id, REAL_PURPOSE)
    event = {"ticket_id": ticket_id, "robot_id": robot_id, "robot_name": name, "command_id": cmd_id,
             "checkpoint_uri": uri, "checkpoint_choice": body.checkpoint, "prompt": prompt, "runs_on": "real",
             "time_cap_s": REAL_TIMEOUT_S, "speed_scale": REAL_SPEED_SCALE, "by": f"human:{who}"}
    await _event(stdb, "deployment.real_run_started", event)
    return {"ok": True, **event}


__all__ = ["REAL_CONFIRM", "REAL_PURPOSE", "RunBody", "deployment_router", "run_deployment"]
