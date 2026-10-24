"""TDM-55 ``POST /stream/token``: a short-lived camera-stream token for the current task claimant.

The browser proves who it is with its own SpacetimeDB identity token (the one the web app keeps in
localStorage), which we check with SpacetimeDB itself (``GET /v1/identity/{identity}/verify``:
204 = this token belongs to this identity). Then:

* the task exists, is ``claimed`` and its lease has not expired,
* the task's assignee is the contributor row bound to that identity,
* the task's robot has published a stream URL (``robot.camera_config_json["stream"]["url"]``,
  written by the robot-client; ``RDF_STREAM_URL`` overrides it for testing).

The answer carries a ``rdf_common.stream_token`` (HMAC, ``RDF_STREAM_TOKEN_SECRET``) scoped to that
robot and task, valid for at most 60 s and never past the claim lease (+10 s for the heartbeat race).
The browser asks again at half-life, so losing the claim cuts the video within about a minute.

The browser reaches this through the web app's server-side proxy (``/api/tdm55/stream-token``),
because the orchestrator's CORS is GET-only. The SpacetimeDB token is never logged or stored.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Awaitable, Callable, Protocol

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .log import get_logger

log = get_logger("stream_api")

stream_router = APIRouter(tags=["stream"])

LEASE_GRACE_S = 10.0
_HEX = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class Tdm55Stdb(Protocol):
    async def sql(self, query: str) -> list[dict]: ...


IdentityVerifier = Callable[[str, str], Awaitable[bool]]


class StreamTokenRequest(BaseModel):
    task_id: int = Field(ge=1)
    identity: str = Field(description="this browser's SpacetimeDB identity, hex")
    spacetime_token: str = Field(min_length=10, max_length=8192, repr=False)


_tdm55_client: Any = None
_tdm55_http: tuple[Any, httpx.AsyncClient] | None = None  # (event loop, keep-alive client)


async def get_stream_stdb() -> Tdm55Stdb:
    global _tdm55_client
    if _tdm55_client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import AsyncSpacetimeClient

        _tdm55_client = AsyncSpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"))
    return _tdm55_client


async def get_identity_verifier() -> IdentityVerifier:
    """``verify(identity_hex, token) -> bool`` against the SpacetimeDB host (tests override this)."""
    global _tdm55_http
    import asyncio

    from rdf_common.settings import SpacetimeSettings, load_settings

    host = load_settings(SpacetimeSettings, "orchestrator").spacetime_host.rstrip("/")
    loop = asyncio.get_running_loop()
    if _tdm55_http is None or _tdm55_http[0] is not loop:  # a client is bound to the loop it was made on
        _tdm55_http = (loop, httpx.AsyncClient(timeout=10.0))
    http = _tdm55_http[1]

    async def verify(identity: str, token: str) -> bool:
        r = await http.get(f"{host}/v1/identity/{identity}/verify",
                                  headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 204:
            return True
        if r.status_code in (400, 401, 403, 404):
            return False
        raise HTTPException(502, f"SpacetimeDB identity check failed ({r.status_code})")

    return verify


def get_stream_secret() -> Any:
    from rdf_common.stream_token import StreamTokenSettings

    try:
        return StreamTokenSettings().rdf_stream_token_secret
    except Exception:  # noqa: BLE001 - pydantic ValidationError; never echo values
        raise HTTPException(503, "RDF_STREAM_TOKEN_SECRET is not set on the orchestrator") from None


def _norm_identity(v: Any) -> str:
    s = str(v or "").lower()
    return s[2:] if s.startswith("0x") else s


def stream_url_of(robot: dict) -> tuple[str | None, dict]:
    """(ws url, stream info) from ``RDF_STREAM_URL`` or ``robot.camera_config_json["stream"]``."""
    try:
        info = (json.loads(robot.get("camera_config_json") or "{}") or {}).get("stream") or {}
    except (json.JSONDecodeError, AttributeError):
        info = {}
    override = os.environ.get("RDF_STREAM_URL", "").strip()
    return (override or info.get("url") or None), info


@stream_router.post("/stream/token")
async def stream_token(body: StreamTokenRequest, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                       verify_identity: IdentityVerifier = Depends(get_identity_verifier),
                       secret: Any = Depends(get_stream_secret)) -> dict:
    from rdf_common.stream_token import DEFAULT_TTL_S, StreamTokenError, mint

    if not _HEX.match(body.identity):
        raise HTTPException(422, "identity must be 64 hex characters")
    identity = _norm_identity(body.identity)
    if not await verify_identity(identity, body.spacetime_token):
        raise HTTPException(401, "SpacetimeDB token does not belong to this identity")

    try:
        tasks = await stdb.sql(f"SELECT * FROM task WHERE id = {int(body.task_id)}")
        task = tasks[0] if tasks else None
        if task is None:
            raise HTTPException(404, f"No task {body.task_id}.")
        if task["status"] != "claimed":
            raise HTTPException(403, f"Task {body.task_id} is {task['status']}, not claimed.")
        lease_us = task.get("lease_expires_at")
        if not lease_us or lease_us / 1e6 <= time.time():
            raise HTTPException(403, "The claim lease has expired; claim the task again.")
        rows = await stdb.sql(f"SELECT * FROM contributor WHERE id = {int(task['assignee_id'])}")
        if not rows or _norm_identity(rows[0].get("identity")) != identity:
            raise HTTPException(403, "Only the contributor who claimed this task can watch its cameras.")
        robots = await stdb.sql(f"SELECT * FROM robot WHERE id = {int(task['robot_id'])}")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}") from None
    if not robots:
        raise HTTPException(404, f"Robot {task['robot_id']} not found.")
    robot = robots[0]
    url, info = stream_url_of(robot)
    if not url:
        raise HTTPException(503, "The robot has not published a camera stream URL yet (is it online?).")

    try:
        token, claims = mint(secret, robot_id=int(robot["id"]), robot_name=str(robot["name"]),
                             task_id=int(task["id"]), sub=f"contributor:{int(task['assignee_id'])}",
                             ttl_s=DEFAULT_TTL_S, not_after=lease_us / 1e6 + LEASE_GRACE_S,
                             claimed_at_us=int(task.get("claimed_at") or 0))  # TDM-57: lease generation
    except StreamTokenError:
        raise HTTPException(403, "The claim lease has expired; claim the task again.") from None
    log.info("stream.token_minted", extra={"task_id": int(task["id"]), "robot_id": int(robot["id"]),
                                           "contributor_id": int(task["assignee_id"]),
                                           "ttl_s": round(claims.exp - claims.iat, 1)})
    return {"token": token, "exp": claims.exp, "stream_url": url, "http_url": info.get("http"),
            "transport": info.get("transport", "jpeg-ws/1"), "cams": info.get("cams") or [],
            "robot_id": int(robot["id"]), "task_id": int(task["id"]), "claimed_at_us": claims.claimed_at_us,
            "lease_expires_at_us": int(lease_us)}
