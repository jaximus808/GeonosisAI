"""Operator robot commands from the website (TDM-78): the robot page's Home / Stop / Run baseline /
Load checkpoint / teleop buttons, the eval trial Pass / Fail verdict (``trial_outcome``), and the soft
e-stop fallback path.

    POST /robots/{robot_id}/commands   {type, payload?, ticket_id?}
      header X-RDF-Operator-Key: <RDF_OPERATOR_KEY>   (required for everything except soft_estop)
      -> 200 {command_id, seq, type, status, issued_by, elapsed_ms}

Why a route: ``issue_robot_command`` only accepts the owner, ``orchestrator``, ``factory`` or a task
claimant, so a browser identity can't call it. The website's Next server (``apps/web`` route
``/api/tdm78/robots/[id]/commands``) forwards here with the operator key, which it keeps server side,
and the orchestrator issues the command under its own identity (``issued_by = orchestrator``). An
``operator.robot_command`` row in event_log (actor ``human:operator``) says it came from the page.

Auth (fail closed): with no ``RDF_OPERATOR_KEY`` configured, every motion command answers 503. The
**soft e-stop needs no key**: stopping the arm is always allowed (the reducer accepts it from any
service role), so a missing or wrong key can never block it.

Errors: 400 type not allowed here, 401 missing/wrong key, 503 key not configured, 409 the reducer
refused (robot busy, in estop, unknown robot), 502 SpacetimeDB unreachable.
"""
from __future__ import annotations

import hmac
import json
import time
from typing import Any, Protocol

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field, SecretStr

from .log import get_logger

log = get_logger("robot_commands_api")

robot_commands_router = APIRouter(tags=["robot-commands"])

# What an operator may send from the page. run_eval is left to the eval flow (TDM-67), which
# owns the eval_run row it needs; reset/takeover commands belong to the marketplace flow.
# trial_outcome (TDM-51): the operator's Pass / Fail {success, note?, eval_run_id, index} for the trial
# the robot is waiting on; the robot refuses a late, duplicate or wrong-trial verdict (command failed).
TDM78_OPERATOR_TYPES = ("home", "stop", "run_baseline", "load_checkpoint", "start_teleop", "stop_teleop", "soft_estop",
                        "trial_outcome")


class RobotCommandRequest(BaseModel):
    type: str = Field(examples=["home"])
    payload: dict[str, Any] = Field(default_factory=dict)
    ticket_id: int = Field(0, ge=0, description="0 = none")


class Tdm78Stdb(Protocol):
    async def sql(self, query: str) -> list[dict]: ...
    async def call_reducer(self, name: str, /, *args: Any) -> None: ...


_tdm78_client: Any = None


async def get_robot_commands_stdb() -> Tdm78Stdb:
    """FastAPI dependency: one shared keep-alive client (tests override this)."""
    global _tdm78_client
    if _tdm78_client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import AsyncSpacetimeClient

        _tdm78_client = AsyncSpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"))
    return _tdm78_client


def get_operator_key() -> str | None:
    """RDF_OPERATOR_KEY from the environment or the repo .env (None = not configured)."""
    from rdf_common.settings import RdfSettings

    class _Tdm78Settings(RdfSettings):
        rdf_operator_key: SecretStr | None = None

    k = _Tdm78Settings().rdf_operator_key
    return k.get_secret_value() if k else None


def _check_key(cmd_type: str, given: str | None, expected: str | None) -> None:
    if cmd_type == "soft_estop":
        return  # never blocked
    if not expected:
        raise HTTPException(503, "operator commands are off: set RDF_OPERATOR_KEY for the orchestrator and the web server")
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "missing or wrong X-RDF-Operator-Key")


@robot_commands_router.post("/robots/{robot_id}/commands")
async def issue_operator_command(
    robot_id: int,
    body: RobotCommandRequest,
    background: BackgroundTasks,
    x_rdf_operator_key: str | None = Header(default=None),
    x_human_id: str | None = Header(default=None),
    stdb: Tdm78Stdb = Depends(get_robot_commands_stdb),
    operator_key: str | None = Depends(get_operator_key),
) -> dict[str, Any]:
    if body.type not in TDM78_OPERATOR_TYPES:
        raise HTTPException(400, f"type must be one of {', '.join(TDM78_OPERATOR_TYPES)}")
    if robot_id < 1:
        raise HTTPException(400, "robot_id must be >= 1")
    _check_key(body.type, x_rdf_operator_key, operator_key)

    t0 = time.perf_counter()
    try:
        await stdb.call_reducer("issue_robot_command", robot_id, body.type, json.dumps(body.payload), body.ticket_id)
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status", None)
        msg = str(getattr(e, "message", "") or e)[:500]
        if isinstance(status, int) and (400 <= status < 500 or status == 530):  # the reducer said no
            log.info("robot_command.refused", extra={"fields": {"robot_id": robot_id, "type": body.type, "error": msg}})
            raise HTTPException(409, msg) from e
        log.warning("robot_command.stdb_down", extra={"fields": {"robot_id": robot_id, "type": body.type, "error": msg}})
        raise HTTPException(502, f"SpacetimeDB unreachable: {msg}") from e
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    # Read the new row back (reducers return nothing): the newest command of this type on the robot.
    row: dict[str, Any] = {}
    try:
        rows = await stdb.sql(f"SELECT * FROM robot_command WHERE robot_id = {int(robot_id)}")
        mine = [r for r in rows if r.get("type") == body.type]
        if mine:
            row = max(mine, key=lambda r: int(r.get("seq") or 0))
    except Exception as e:  # noqa: BLE001 - the command went out; the id is a nicety
        log.warning("robot_command.readback_failed", extra={"fields": {"error": str(e)[:200]}})

    who = (x_human_id or "operator")[:64]
    background.add_task(_audit, stdb, robot_id, body.type, row.get("id"), who)
    log.info("robot_command.issued", extra={"fields": {"robot_id": robot_id, "type": body.type, "command_id": row.get("id"), "elapsed_ms": elapsed_ms}})
    return {
        "command_id": int(row["id"]) if row.get("id") is not None else None,
        "seq": int(row["seq"]) if row.get("seq") is not None else None,
        "type": body.type,
        "status": row.get("status"),
        "issued_by": row.get("issued_by", "orchestrator"),
        "elapsed_ms": elapsed_ms,
        "path": "orchestrator",
    }


async def _audit(stdb: Tdm78Stdb, robot_id: int, cmd_type: str, command_id: Any, who: str) -> None:
    try:
        await stdb.call_reducer("append_event", "human:operator", "operator.robot_command",
                                json.dumps({"robot_id": robot_id, "type": cmd_type, "command_id": command_id,
                                            "via": "web", "who": who}))
    except Exception as e:  # noqa: BLE001 - evidence is best effort
        log.warning("robot_command.audit_failed", extra={"fields": {"error": str(e)[:200]}})
