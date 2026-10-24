"""[SIM] Practice sessions on a simulated robot: drive the arm with no task, no lease, no recording.

For the MuJoCo sim robot (``rdf-robot-client --adapter mujoco``) and the TDM-49 ``--sim`` arm only;
every route refuses a real robot (``stream_api.robot_is_sim``).

  POST /robots/{id}/practice/start   {identity?, spacetime_token?}
       -> issues ``start_teleop`` {kind: "practice", task_id: 0, practice: true} on the robot (it must be
          online with no open job) and returns a control token for /ws/control + /ws/state:
          {token, exp, control_url, state_url, stream_url, cams, robot_id, command_id, practice: true, sim: true}
  POST /robots/{id}/practice/token   {identity?, spacetime_token?}   a fresh token for the same session
  POST /robots/{id}/practice/stop    -> ``stop_teleop`` (ends the practice session; the arm holds)
  POST /robots/{id}/sim/reset_scene  -> ``reset_request`` {sim_reset: true} when the robot is idle (the
       simulator re-places the scene's objects); during a practice session the browser sends
       ``{t:"reset_scene"}`` on /ws/control instead.

Who may: an operator (``X-RDF-Operator-Key``, added by the web server after operator login) or any
registered contributor proving their SpacetimeDB identity (``identity`` + ``spacetime_token``, like
``POST /stream/token``). The token's subject is ``practice:operator`` / ``practice:contributor:<id>``
with task_id 0; the robot accepts it only for practice (no lease, no recording, reset_scene allowed).
"""
from __future__ import annotations

import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .log import get_logger
from .stream_api import (
    IdentityVerifier,
    Tdm55Stdb,
    _HEX,
    _norm_identity,
    get_identity_verifier,
    get_stream_operator_key,
    get_stream_secret,
    get_stream_stdb,
    robot_is_sim,
    state_url_of,
    stream_url_of,
)

log = get_logger("practice_api")
practice_router = APIRouter(tags=["practice"])
OPEN = ("issued", "acked")
JOBS = ("run_baseline", "run_eval", "start_teleop", "reset_request", "home", "load_checkpoint")


class PracticeRequest(BaseModel):
    identity: str | None = Field(default=None, description="contributor's SpacetimeDB identity, hex")
    spacetime_token: str | None = Field(default=None, min_length=10, max_length=8192, repr=False)


async def _who(body: PracticeRequest, stdb: Tdm55Stdb, verify: IdentityVerifier, operator_key: str | None,
               given_key: str | None) -> str:
    if operator_key and given_key and hmac.compare_digest(given_key.encode(), operator_key.encode()):
        return "operator"
    if not body.identity or not body.spacetime_token:
        raise HTTPException(401, "practice needs operator login, or a contributor identity + spacetime_token")
    if not _HEX.match(body.identity):
        raise HTTPException(422, "identity must be 64 hex characters")
    ident = _norm_identity(body.identity)
    if not await verify(ident, body.spacetime_token):
        raise HTTPException(401, "SpacetimeDB token does not belong to this identity")
    rows = await stdb.sql("SELECT * FROM contributor")
    me = next((r for r in rows if _norm_identity(r.get("identity")) == ident), None)
    if me is None:
        raise HTTPException(403, "join as a contributor first (/join)")
    return f"contributor:{int(me['id'])}"


async def _sim_robot(stdb: Tdm55Stdb, robot_id: int) -> dict:
    rows = await stdb.sql(f"SELECT * FROM robot WHERE id = {int(robot_id)}")
    if not rows:
        raise HTTPException(404, f"Robot {robot_id} not found.")
    robot = rows[0]
    if not robot_is_sim(robot):
        raise HTTPException(403, "practice sessions run on simulated robots only")
    return robot


async def _open_jobs(stdb: Tdm55Stdb, robot_id: int) -> list[dict]:
    rows = await stdb.sql(f"SELECT * FROM robot_command WHERE robot_id = {int(robot_id)}")
    return [r for r in rows if r.get("status") in OPEN and r.get("type") in JOBS]


def _token(robot: dict, who: str, secret: Any) -> dict:
    from rdf_common.stream_token import DEFAULT_TTL_S, mint

    url, info = stream_url_of(robot)
    token, claims = mint(secret, robot_id=int(robot["id"]), robot_name=str(robot["name"]), task_id=0,
                         sub=f"practice:{who}", ttl_s=DEFAULT_TTL_S, scope="control")
    return {"token": token, "exp": claims.exp, "stream_url": url, "control_url": info.get("control_url"),
            "state_url": state_url_of(url, info), "http_url": info.get("http"), "cams": info.get("cams") or [],
            "robot_id": int(robot["id"]), "task_id": 0, "practice": True, "sim": True,
            "note": "[SIM] practice session: no task, no lease, nothing is recorded"}


async def _issue(stdb: Any, robot_id: int, cmd: str, payload: dict) -> int | None:
    try:
        await stdb.call_reducer("issue_robot_command", int(robot_id), cmd, json.dumps(payload), 0)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"issue_robot_command({cmd}) failed: {str(e)[:200]}") from None
    rows = await stdb.sql(f"SELECT * FROM robot_command WHERE robot_id = {int(robot_id)}")
    mine = [r for r in rows if r.get("type") == cmd]
    return int(max(mine, key=lambda r: int(r["id"]))["id"]) if mine else None


@practice_router.post("/robots/{robot_id}/practice/start")
async def practice_start(robot_id: int, body: PracticeRequest, stdb: Any = Depends(get_stream_stdb),
                         verify: IdentityVerifier = Depends(get_identity_verifier), secret: Any = Depends(get_stream_secret),
                         operator_key: str | None = Depends(get_stream_operator_key),
                         x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    who = await _who(body, stdb, verify, operator_key, x_rdf_operator_key)
    robot = await _sim_robot(stdb, robot_id)
    if robot.get("status") not in ("online", "idle"):
        raise HTTPException(409, f"robot {robot_id} is {robot.get('status')}; practice needs it online and idle")
    jobs = await _open_jobs(stdb, robot_id)
    practice = [j for j in jobs if j.get("type") == "start_teleop"
                and (json.loads(j.get("payload_json") or "{}") or {}).get("practice")]
    if jobs and not practice:
        raise HTTPException(409, f"robot {robot_id} is busy ({jobs[0]['type']} #{jobs[0]['id']}); try again when it is idle")
    cmd_id = int(practice[0]["id"]) if practice else await _issue(
        stdb, robot_id, "start_teleop", {"kind": "practice", "task_id": 0, "practice": True, "by": f"practice:{who}",
                                         "note": "[SIM] practice session: no lease, no recording"})
    log.info("practice.started", extra={"robot_id": robot_id, "who": who, "command_id": cmd_id})
    return {**_token(robot, who, secret), "command_id": cmd_id, "joined_existing": bool(practice)}


@practice_router.post("/robots/{robot_id}/practice/token")
async def practice_token(robot_id: int, body: PracticeRequest, stdb: Any = Depends(get_stream_stdb),
                         verify: IdentityVerifier = Depends(get_identity_verifier), secret: Any = Depends(get_stream_secret),
                         operator_key: str | None = Depends(get_stream_operator_key),
                         x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    who = await _who(body, stdb, verify, operator_key, x_rdf_operator_key)
    return _token(await _sim_robot(stdb, robot_id), who, secret)


@practice_router.post("/robots/{robot_id}/practice/stop")
async def practice_stop(robot_id: int, body: PracticeRequest, stdb: Any = Depends(get_stream_stdb),
                        verify: IdentityVerifier = Depends(get_identity_verifier),
                        operator_key: str | None = Depends(get_stream_operator_key),
                        x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    who = await _who(body, stdb, verify, operator_key, x_rdf_operator_key)
    await _sim_robot(stdb, robot_id)
    practice = [j for j in await _open_jobs(stdb, robot_id) if j.get("type") == "start_teleop"
                and (json.loads(j.get("payload_json") or "{}") or {}).get("practice")]
    if not practice:
        return {"ok": True, "stopped": False, "detail": "no practice session running"}
    cmd_id = await _issue(stdb, robot_id, "stop_teleop", {"reason": f"practice ended by practice:{who}"})
    return {"ok": True, "stopped": True, "command_id": cmd_id}


@practice_router.post("/robots/{robot_id}/sim/reset_scene")
async def sim_reset_scene(robot_id: int, body: PracticeRequest, stdb: Any = Depends(get_stream_stdb),
                          verify: IdentityVerifier = Depends(get_identity_verifier),
                          operator_key: str | None = Depends(get_stream_operator_key),
                          x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    who = await _who(body, stdb, verify, operator_key, x_rdf_operator_key)
    await _sim_robot(stdb, robot_id)
    jobs = await _open_jobs(stdb, robot_id)
    if jobs:
        raise HTTPException(409, f"robot {robot_id} is running {jobs[0]['type']} #{jobs[0]['id']}; in a practice "
                                 "session send {t:\"reset_scene\"} on /ws/control instead")
    cmd_id = await _issue(stdb, robot_id, "reset_request", {"sim_reset": True, "reason": f"[SIM] scene reset by {who}"})
    return {"ok": True, "command_id": cmd_id}


__all__ = ["practice_router"]
