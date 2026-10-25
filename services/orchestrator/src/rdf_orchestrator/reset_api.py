"""TDM-62 reset task HTTP surface (the phone page ``apps/web/src/app/reset/[taskId]`` uses it).

  GET  /resets/{task_id}         public: the robot's reset routine (steps + signed start-state photo
                                 URL; stored > TDM-32 onboarding > default, ``reset_routine``) and the
                                 scene photos attached to this task
  POST /resets/{task_id}/done    operator only (X-RDF-Operator-Key): the cockpit's "Done, scene reset" button
                                 finishes a real robot's reset task (reset_done.py), so the eval resumes
  POST /resets/{task_id}/photo   claimant only: attach a photo of the reset scene as task evidence
        {identity, spacetime_token, source: "camera" | "upload", image_b64?}
        camera: the orchestrator mints a short TDM-55 stream token and fetches
                ``<stream http>/snapshot/<cam>.jpg`` from the robot (no browser CORS / token juggling)
        upload: a photo the person took with their phone (JPEG/PNG, <= 6 MB, base64)
        -> {storage_uri, url, source, cam?}

The claimant check is TDM-55's: the SpacetimeDB token must belong to ``identity``, the task must be
``claimed`` with a live lease, and its assignee must be that identity's contributor. The page puts
the returned ``storage_uri`` in ``complete_task``'s result_json (``photo_uri``), so the photo is in
the ``task.completed`` event_log row for the reviewer / ops. The SpacetimeDB token is never logged.

Run standalone for local proofs (media + stream token + resets, no bus handlers):

    cd services/orchestrator && uv run uvicorn rdf_orchestrator.reset_api:app --port 8062
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import reset_done as rdone
from . import reset_routine as rr
from .robot_commands_api import Tdm78Stdb, _check_key, get_operator_key, get_robot_commands_stdb
from .log import get_logger
from .stream_api import (_HEX, LEASE_GRACE_S, IdentityVerifier, Tdm55Stdb, _norm_identity, get_identity_verifier,
                         get_stream_secret, get_stream_stdb, stream_url_of)

log = get_logger("reset_api")

reset_router = APIRouter(tags=["resets"])

PHOTO_URL_TTL_S = 15 * 60
PREFERRED_CAMS = ("exo", "top", "front", "scene")
_snap_http: tuple[Any, httpx.AsyncClient] | None = None


class ResetPhotoRequest(BaseModel):
    identity: str = Field(description="this browser's SpacetimeDB identity, hex")
    spacetime_token: str = Field(min_length=10, max_length=8192, repr=False)
    source: str = Field(default="camera", pattern="^(camera|upload)$")
    image_b64: str | None = Field(default=None, max_length=9_000_000, repr=False)


def get_stream_secret_dep() -> Any:
    """The TDM-55 stream token secret (tests monkeypatch ``reset_api.get_stream_secret_dep``)."""
    return get_stream_secret()


def get_storage() -> Any:
    from . import storage

    return storage.get_backend()


async def _snap_client() -> httpx.AsyncClient:
    global _snap_http
    loop = asyncio.get_running_loop()
    if _snap_http is None or _snap_http[0] is not loop:
        _snap_http = (loop, httpx.AsyncClient(timeout=8.0))
    return _snap_http[1]


def http_base_of(stream_url: str, info: dict) -> str:
    """The robot stream server's HTTP base (``info.http``, else derived from the ws URL)."""
    if info.get("http"):
        return str(info["http"]).rstrip("/")
    base = stream_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    for suffix in ("/ws/cameras", "/ws"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/")


def pick_cam(info: dict) -> list[str]:
    cams = [str(c) for c in (info.get("cams") or [])]
    ordered = [c for c in PREFERRED_CAMS if c in cams] + [c for c in cams if c not in PREFERRED_CAMS]
    return ordered or list(PREFERRED_CAMS)


async def _one(stdb: Tdm55Stdb, sql: str) -> dict | None:
    rows = await stdb.sql(sql)
    return rows[0] if rows else None


async def claimant_task(task_id: int, identity: str, token: str, stdb: Tdm55Stdb,
                        verify: IdentityVerifier) -> tuple[dict, dict]:
    """(task, robot) when ``identity`` holds a live claim on reset task ``task_id``; else HTTPException."""
    if not _HEX.match(identity):
        raise HTTPException(422, "identity must be 64 hex characters")
    ident = _norm_identity(identity)
    if not await verify(ident, token):
        raise HTTPException(401, "SpacetimeDB token does not belong to this identity")
    try:
        task = await _one(stdb, f"SELECT * FROM task WHERE id = {int(task_id)}")
        if task is None:
            raise HTTPException(404, f"No task {task_id}.")
        if task.get("type") != "reset":
            raise HTTPException(409, f"Task {task_id} is a {task.get('type')} task, not a reset.")
        if task["status"] != "claimed":
            raise HTTPException(403, f"Task {task_id} is {task['status']}, not claimed.")
        lease_us = task.get("lease_expires_at")
        if not lease_us or lease_us / 1e6 <= time.time():
            raise HTTPException(403, "The claim lease has expired; claim the task again.")
        c = await _one(stdb, f"SELECT * FROM contributor WHERE id = {int(task['assignee_id'])}")
        if c is None or _norm_identity(c.get("identity")) != ident:
            raise HTTPException(403, "Only the person who claimed this reset can attach photos to it.")
        robot = await _one(stdb, f"SELECT * FROM robot WHERE id = {int(task['robot_id'])}")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}") from None
    if robot is None:
        raise HTTPException(404, f"Robot {task['robot_id']} not found.")
    return task, robot


async def camera_snapshot(task: dict, robot: dict, secret: Any) -> tuple[bytes, str]:
    """One JPEG from the robot's TDM-55 stream server (``/snapshot/<cam>.jpg``)."""
    from rdf_common.stream_token import StreamTokenError, mint

    url, info = stream_url_of(robot)
    if not url:
        raise HTTPException(503, "The robot has not published a camera stream yet, so no photo.")
    try:
        token, _ = mint(secret, robot_id=int(robot["id"]), robot_name=str(robot["name"]), task_id=int(task["id"]),
                        sub=f"contributor:{int(task['assignee_id'])}", ttl_s=20,
                        not_after=int(task["lease_expires_at"]) / 1e6 + LEASE_GRACE_S,
                        claimed_at_us=int(task.get("claimed_at") or 0))
    except StreamTokenError:
        raise HTTPException(403, "The claim lease has expired; claim the task again.") from None
    base = http_base_of(url, info)
    http = await _snap_client()
    last = "no camera answered"
    for cam in pick_cam(info):
        try:
            r = await http.get(f"{base}/snapshot/{cam}.jpg", params={"token": token})
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}"
            continue
        if r.status_code == 200 and rr.is_image(r.content):
            return r.content, cam
        last = f"{cam}: HTTP {r.status_code} {r.text[:40].strip()}".strip()
    raise HTTPException(502, f"Could not get a photo from the robot camera ({last}).")


@reset_router.post("/resets/{task_id}/photo")
async def reset_photo(task_id: int, body: ResetPhotoRequest, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                      verify: IdentityVerifier = Depends(get_identity_verifier),
                      storage_backend: Any = Depends(get_storage)) -> dict:
    task, robot = await claimant_task(task_id, body.identity, body.spacetime_token, stdb, verify)
    cam = None
    if body.source == "camera":
        data, cam = await camera_snapshot(task, robot, get_stream_secret_dep())
    else:
        try:
            data = base64.b64decode((body.image_b64 or "").split(",")[-1], validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, "image_b64 is not valid base64") from None
        if not rr.is_image(data) or len(data) > rr.MAX_PHOTO_BYTES:
            raise HTTPException(422, "The photo must be a JPEG or PNG under 6 MB.")
    key = rr.evidence_key(int(task["ticket_id"]), int(task["id"]), body.source)
    try:
        uri = await asyncio.to_thread(storage_backend.put, key, data, "image/jpeg")
        url = (await asyncio.to_thread(storage_backend.signed_get, uri, ttl_s=PHOTO_URL_TTL_S)).url
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not store the photo ({type(e).__name__}).") from None
    log.info("reset.photo_stored", extra={"fields": {"task_id": int(task["id"]), "robot_id": int(robot["id"]),
                                                     "source": body.source, "cam": cam, "bytes": len(data),
                                                     "storage_uri": uri}})
    return {"storage_uri": uri, "url": url, "source": body.source, "cam": cam, "bytes": len(data)}


def get_reset_contributor() -> int:
    """The operator's contributor id for resets (RDF_RESET_TEXT_CONTRIBUTOR; tests override)."""
    return rdone.reset_contributor()


@reset_router.post("/resets/{task_id}/done")
async def reset_done(task_id: int, x_rdf_operator_key: str | None = Header(default=None),
                     stdb: Tdm78Stdb = Depends(get_robot_commands_stdb),
                     operator_key: str | None = Depends(get_operator_key),
                     contributor_id: int = Depends(get_reset_contributor)) -> dict:
    _check_key("reset_done", x_rdf_operator_key, operator_key)
    try:
        task = await _one(stdb, f"SELECT * FROM task WHERE id = {int(task_id)}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}") from None
    if task is None:
        raise HTTPException(404, f"No task {task_id}.")
    if task.get("type") != "reset":
        raise HTTPException(409, f"Task {task_id} is a {task.get('type')} task, not a reset.")
    try:
        out = await rdone.run_async(stdb.call_reducer, task, contributor_id, via="cockpit")
    except Exception as e:  # noqa: BLE001 - the reducer said no (e.g. it was completed a moment ago)
        raise HTTPException(409, f"Could not finish the reset: {str(e)[:200]}") from None
    if not out.get("already"):
        try:
            await stdb.call_reducer("append_event", rdone.ACTOR, rdone.EV_DONE, json.dumps(
                {"ticket_id": int(task.get("ticket_id") or 0), "robot_id": int(task.get("robot_id") or 0),
                 "task_id": int(task_id), "via": "cockpit", "mode": out.get("mode"),
                 "contributor_id": contributor_id or None}, separators=(",", ":")))
        except Exception:  # noqa: BLE001 - the audit row is best effort
            pass
    log.info("reset.operator_done", extra={"fields": {"task_id": int(task_id), "mode": out.get("mode"),
                                                      "already": out.get("already")}})
    return out


@reset_router.get("/resets/{task_id}")
async def reset_info(task_id: int, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                     storage_backend: Any = Depends(get_storage)) -> dict:
    try:
        task = await _one(stdb, f"SELECT * FROM task WHERE id = {int(task_id)}")
        ticket = await _one(stdb, f"SELECT * FROM ticket WHERE id = {int(task['ticket_id'])}") if task else None
        robot = await _one(stdb, f"SELECT * FROM robot WHERE id = {int(task['robot_id'])}") if task else None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}") from None
    if task is None:
        raise HTTPException(404, f"No task {task_id}.")
    if task.get("type") != "reset":
        raise HTTPException(409, f"Task {task_id} is a {task.get('type')} task, not a reset.")
    routine = await asyncio.to_thread(rr.load, int(task["robot_id"]), (ticket or {}).get("task_description") or "",
                                      onboarding=rr.onboarding_routine((robot or {}).get("camera_config_json")),
                                      backend=storage_backend)
    ref_url = None
    evidence: list[dict] = []
    try:
        if routine.reference_uri:
            ref_url = (await asyncio.to_thread(storage_backend.signed_get, routine.reference_uri,
                                               ttl_s=PHOTO_URL_TTL_S)).url
        objs = await asyncio.to_thread(storage_backend.list, rr.evidence_prefix(int(task["ticket_id"]), int(task["id"])),
                                       limit=20)
        for o in sorted(objs, key=lambda o: o.key):
            name = o.key.rsplit("/", 1)[-1]
            evidence.append({"storage_uri": o.storage_uri, "source": name.split("-", 1)[0],
                             "url": (await asyncio.to_thread(storage_backend.signed_get, o.storage_uri,
                                                             ttl_s=PHOTO_URL_TTL_S)).url,
                             "at_ms": o.mtime * 1000 if o.mtime < 10**11 else o.mtime})
    except Exception as e:  # noqa: BLE001 - photos are optional; the steps still render
        log.info("reset.photos_unavailable", extra={"fields": {"task_id": task_id, "why": f"{type(e).__name__}"}})
    return {"task_id": int(task["id"]), "robot_id": int(task["robot_id"]), "ticket_id": int(task["ticket_id"]),
            "routine": {"steps": routine.steps, "source": routine.source, "reference_url": ref_url,
                        "updated_at": routine.updated_at, "prompt": routine.prompt, "est_seconds": routine.est_seconds},
            "evidence": evidence}


def _standalone() -> FastAPI:
    from fastapi.middleware.cors import CORSMiddleware

    from .media_api import media_router
    from .stream_api import stream_router

    a = FastAPI(title="rdf-orchestrator (tdm62 resets, standalone)")
    a.include_router(media_router)
    a.include_router(stream_router)
    a.include_router(reset_router)
    origins = ["http://localhost:3737", "http://127.0.0.1:3737"] + [
        o.strip() for o in os.environ.get("RDF_WEB_ORIGINS", "").split(",") if o.strip()]
    a.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET"], allow_headers=["*"])
    return a


app = _standalone()

__all__ = ["app", "camera_snapshot", "claimant_task", "http_base_of", "pick_cam", "reset_router"]
