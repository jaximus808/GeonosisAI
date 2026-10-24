"""TDM-57: operator revoke of a live arm-control lease, for the dashboard robot page.

    POST /tasks/{task_id}/revoke   {reason?, cancel?: bool}
      header X-RDF-Operator-Key: <RDF_OPERATOR_KEY>
      -> 200 {task_id, action: "released"|"cancelled", previous_assignee_id, robot_id}

Why a route: ``release_task`` (for someone else's claim) and ``cancel_task`` only accept the owner or the
``orchestrator`` service identity (TDM-10), so a browser can't call them. The web app's Next server
(``/api/tdm57/tasks/[id]/revoke``) forwards here with the operator key it keeps server side.

* ``cancel: false`` (default) calls ``release_task``: control ends and the task goes back to the
  market, so the same or another contributor may claim it again.
* ``cancel: true`` calls ``cancel_task(task_id, reason)``: control ends and the task is withdrawn.

Either way the task row stops being a live claim, which ends control everywhere: the teleop page
(``useTeleopLease``) disables its controls on the row update, the stream/control token can't be
refreshed (``POST /stream/token`` refuses), and the robot's ``/control`` guard
(``rdf_robot_client.tdm57_lease``) re-reads the row within 2 s and holds the arm.

Auth fails closed: no ``RDF_OPERATOR_KEY`` configured -> 503. An ``operator.control_revoked`` event
(actor ``human:operator``) records who asked; the reducer writes its own ``task.released`` /
``task.cancelled`` row.
"""
from __future__ import annotations

import hmac
import json
from typing import Any, Protocol

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field, SecretStr

from .log import get_logger

log = get_logger("tdm57_control_api")

tdm57_control_router = APIRouter(tags=["teleop-control"])


class RevokeRequest(BaseModel):
    reason: str = Field("operator revoke", max_length=500)
    cancel: bool = Field(False, description="true = cancel_task (withdraw); false = release_task (reopen)")


class Tdm57Stdb(Protocol):
    async def sql(self, query: str) -> list[dict]: ...
    async def call_reducer(self, name: str, /, *args: Any) -> None: ...


_tdm57_client: Any = None


async def get_tdm57_stdb() -> Tdm57Stdb:
    global _tdm57_client
    if _tdm57_client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import AsyncSpacetimeClient

        _tdm57_client = AsyncSpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"))
    return _tdm57_client


def get_tdm57_operator_key() -> str | None:
    from rdf_common.settings import RdfSettings

    class _Tdm57Settings(RdfSettings):
        rdf_operator_key: SecretStr | None = None

    k = _Tdm57Settings().rdf_operator_key
    return k.get_secret_value() if k else None


@tdm57_control_router.post("/tasks/{task_id}/revoke")
async def revoke_control(
    task_id: int,
    body: RevokeRequest,
    background: BackgroundTasks,
    x_rdf_operator_key: str | None = Header(default=None),
    x_human_id: str | None = Header(default=None),
    stdb: Tdm57Stdb = Depends(get_tdm57_stdb),
    operator_key: str | None = Depends(get_tdm57_operator_key),
) -> dict[str, Any]:
    if not operator_key:
        raise HTTPException(503, "revoke is off: set RDF_OPERATOR_KEY for the orchestrator and the web server")
    if not x_rdf_operator_key or not hmac.compare_digest(x_rdf_operator_key.encode(), operator_key.encode()):
        raise HTTPException(401, "missing or wrong X-RDF-Operator-Key")
    if task_id < 1:
        raise HTTPException(400, "task_id must be >= 1")

    try:
        rows = await stdb.sql(f"SELECT * FROM task WHERE id = {int(task_id)}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB unreachable: {type(e).__name__}") from None
    if not rows:
        raise HTTPException(404, f"No task {task_id}.")
    task = rows[0]
    if body.cancel:
        if task.get("status") not in ("open", "claimed"):
            raise HTTPException(409, f"Task {task_id} is {task.get('status')}; nothing to cancel.")
        reducer, args, action = "cancel_task", (task_id, body.reason or "operator revoke"), "cancelled"
    else:
        if task.get("status") != "claimed":
            raise HTTPException(409, f"Task {task_id} is {task.get('status')}; nobody holds control.")
        reducer, args, action = "release_task", (task_id,), "released"

    try:
        await stdb.call_reducer(reducer, *args)
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status", None)
        msg = str(getattr(e, "message", "") or e)[:500]
        if isinstance(status, int) and (400 <= status < 500 or status == 530):
            raise HTTPException(409, msg) from e
        raise HTTPException(502, f"SpacetimeDB unreachable: {msg}") from e

    who = (x_human_id or "operator")[:64]
    out = {"task_id": task_id, "action": action, "previous_assignee_id": int(task.get("assignee_id") or 0),
           "robot_id": int(task.get("robot_id") or 0)}
    background.add_task(_audit, stdb, {**out, "reason": body.reason, "who": who, "via": "web"})
    log.info("control.revoked", extra={"fields": out})
    return out


async def _audit(stdb: Tdm57Stdb, payload: dict[str, Any]) -> None:
    try:
        await stdb.call_reducer("append_event", "human:operator", "operator.control_revoked", json.dumps(payload))
    except Exception as e:  # noqa: BLE001 - the reducer already logged task.released/cancelled
        log.warning("control.revoke_audit_failed", extra={"fields": {"error": str(e)[:200]}})
