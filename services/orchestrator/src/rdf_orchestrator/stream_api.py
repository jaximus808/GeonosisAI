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

TDM-96, ``scope: "view"``: a read-only token (cameras + ``/ws/state``; the robot's ``/control`` refuses
it) for watching a robot without driving it. Two ways to get one:

* **operator**: ``{scope:"view", robot_id}`` plus ``X-RDF-Operator-Key`` (the web server adds it after
  ``requireOperator()``, i.e. operator login; ``/api/robotview/state-token``). ``sub`` = ``operator:view``.
* **claimant**: ``{scope:"view", task_id, identity, spacetime_token}``, the same checks as a control
  token (live claim, assignee = this identity). Bound to the task's robot and lease like a control token.

Anything else is refused (401/403). ``scope`` defaults to ``control``: the TDM-55 behaviour, unchanged.
The answer adds ``state_url`` (the robot's ``/ws/state``, from ``camera_config_json.stream.state_url``,
else derived from the camera URL) and ``scope``.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import time
from typing import Any, Awaitable, Callable, Literal, Protocol

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, SecretStr

from .log import get_logger

log = get_logger("stream_api")

stream_router = APIRouter(tags=["stream"])

LEASE_GRACE_S = 10.0
_HEX = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class Tdm55Stdb(Protocol):
    async def sql(self, query: str) -> list[dict]: ...


IdentityVerifier = Callable[[str, str], Awaitable[bool]]


class StreamTokenRequest(BaseModel):
    task_id: int | None = Field(default=None, ge=1)
    identity: str | None = Field(default=None, description="this browser's SpacetimeDB identity, hex")
    spacetime_token: str | None = Field(default=None, min_length=10, max_length=8192, repr=False)
    scope: Literal["control", "view"] = "control"  # TDM-96
    robot_id: int | None = Field(default=None, ge=1, description="view scope via operator key: the robot to watch")


def get_stream_operator_key() -> str | None:
    """RDF_OPERATOR_KEY (env or repo .env), the key the web server adds after operator login (TDM-96)."""
    from rdf_common.settings import RdfSettings

    class _Tdm96Settings(RdfSettings):
        rdf_operator_key: SecretStr | None = None

    k = _Tdm96Settings().rdf_operator_key
    return k.get_secret_value() if k else None


def state_url_of(url: str | None, info: dict) -> str | None:
    """The robot's ``/ws/state`` URL: published ``state_url``, else the camera URL with the path swapped."""
    if os.environ.get("RDF_STATE_URL", "").strip():
        return os.environ["RDF_STATE_URL"].strip()
    if info.get("state_url"):
        return str(info["state_url"])
    if url and url.endswith("/ws/cameras"):
        return url[: -len("/ws/cameras")] + "/ws/state"
    return None


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


def robot_is_sim(robot: dict) -> bool:
    """A simulated robot (``camera_config_json.sim`` or embodiment ``sim*``): no camera stream is required."""
    try:
        cfg = json.loads(robot.get("camera_config_json") or "{}") or {}
    except (json.JSONDecodeError, AttributeError):
        cfg = {}
    return bool(cfg.get("sim")) or str(robot.get("embodiment") or "").startswith("sim")


def stream_url_of(robot: dict) -> tuple[str | None, dict]:
    """(ws url, stream info) from ``RDF_STREAM_URL`` or ``robot.camera_config_json["stream"]``."""
    try:
        info = (json.loads(robot.get("camera_config_json") or "{}") or {}).get("stream") or {}
    except (json.JSONDecodeError, AttributeError):
        info = {}
    override = os.environ.get("RDF_STREAM_URL", "").strip()
    return (override or info.get("url") or None), info


async def _operator_view_token(body: StreamTokenRequest, stdb: Tdm55Stdb, secret: Any) -> dict:
    """TDM-96: a view-only token for an operator (key already checked), not bound to any task."""
    from rdf_common.stream_token import DEFAULT_TTL_S, mint

    try:
        robots = await stdb.sql(f"SELECT * FROM robot WHERE id = {int(body.robot_id or 0)}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}") from None
    if not robots:
        raise HTTPException(404, f"Robot {body.robot_id} not found.")
    robot = robots[0]
    url, info = stream_url_of(robot)
    if not url and not robot_is_sim(robot):  # [SIM] robots are driven from /ws/state renders, not video
        raise HTTPException(503, "The robot has not published a stream URL yet (is it online?).")
    token, claims = mint(secret, robot_id=int(robot["id"]), robot_name=str(robot["name"]), task_id=0,
                         sub="operator:view", ttl_s=DEFAULT_TTL_S, scope="view")
    log.info("stream.view_token_minted", extra={"robot_id": int(robot["id"]), "via": "operator"})
    return {"token": token, "exp": claims.exp, "scope": "view", "stream_url": url, "http_url": info.get("http"),
            "state_url": state_url_of(url, info), "transport": info.get("transport", "jpeg-ws/1"),
            "cams": info.get("cams") or [], "robot_id": int(robot["id"]), "task_id": 0, "sim": bool(
                (json.loads(robot.get("camera_config_json") or "{}") or {}).get("sim"))}


@stream_router.post("/stream/token")
async def stream_token(body: StreamTokenRequest, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                       verify_identity: IdentityVerifier = Depends(get_identity_verifier),
                       secret: Any = Depends(get_stream_secret),
                       operator_key: str | None = Depends(get_stream_operator_key),
                       x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    from rdf_common.stream_token import DEFAULT_TTL_S, StreamTokenError, mint

    if body.scope == "view" and body.task_id is None:  # TDM-96 operator path
        if body.robot_id is None:
            raise HTTPException(422, "a view token needs robot_id (operator) or task_id + identity (claimant)")
        if not operator_key:
            raise HTTPException(503, "operator view tokens are off: set RDF_OPERATOR_KEY")
        if not x_rdf_operator_key or not hmac.compare_digest(x_rdf_operator_key.encode(), operator_key.encode()):
            raise HTTPException(401, "operator login required to watch a robot without a claimed task")
        return await _operator_view_token(body, stdb, secret)
    if body.task_id is None or not body.identity or not body.spacetime_token:
        raise HTTPException(422, "task_id, identity and spacetime_token are required")

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
    if not url and not robot_is_sim(robot):  # [SIM] no camera gate: the browser renders the sim from /ws/state
        raise HTTPException(503, "The robot has not published a camera stream URL yet (is it online?).")

    try:
        token, claims = mint(secret, robot_id=int(robot["id"]), robot_name=str(robot["name"]),
                             task_id=int(task["id"]), sub=f"contributor:{int(task['assignee_id'])}",
                             ttl_s=DEFAULT_TTL_S, not_after=lease_us / 1e6 + LEASE_GRACE_S,
                             claimed_at_us=int(task.get("claimed_at") or 0),  # TDM-57: lease generation
                             scope=body.scope)  # TDM-96: "view" = read-only
    except StreamTokenError:
        raise HTTPException(403, "The claim lease has expired; claim the task again.") from None
    log.info("stream.token_minted", extra={"task_id": int(task["id"]), "robot_id": int(robot["id"]),
                                           "contributor_id": int(task["assignee_id"]),
                                           "ttl_s": round(claims.exp - claims.iat, 1)})
    return {"token": token, "exp": claims.exp, "stream_url": url, "http_url": info.get("http"),
            "transport": info.get("transport", "jpeg-ws/1"), "cams": info.get("cams") or [],
            "robot_id": int(robot["id"]), "task_id": int(task["id"]), "claimed_at_us": claims.claimed_at_us,
            "lease_expires_at_us": int(lease_us), "scope": claims.scope, "state_url": state_url_of(url, info)}
