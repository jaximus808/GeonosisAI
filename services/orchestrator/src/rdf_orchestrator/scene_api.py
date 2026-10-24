"""TDM-104 scene routes: a ticket's generated 3D task scene, and deploying it to the sim robot.

    GET  /tickets/{id}/scene[?revision=N]   newest (or revision N) spec + every revision + the active deploy
    POST /tickets/{id}/scene/generate       {hint?}            Claude writes a new revision        (operator key)
    PUT  /tickets/{id}/scene                {spec}             store a hand-edited revision        (operator key)
    POST /tickets/{id}/scene/deploy         {revision?, robot_id?}  tell the sim robot to load it  (operator key)
    GET  /robots/{id}/scene                 what the robot was told to load + did it confirm (pending|loaded|failed)
    POST /scenes/validate                   {spec} -> {ok, spec (normalized), errors, success}
    GET  /scenes/default                    the built-in cube + plate scene

Operator routes take ``X-RDF-Operator-Key`` (same key and fail-closed rules as TDM-78's robot commands);
the website forwards with it from ``/api/tdm104/...``. Storage and deploy semantics: ``scene.py``.
Errors: 400 bad body, 401/503 key, 404 no ticket / no scene, 409 no sim robot to deploy to, 422 invalid spec
or the generated scene did not validate, 502 SpacetimeDB or Claude unreachable.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from rdf_common import scene_spec as ss

from . import scene as sc
from .log import get_logger
from .robot_commands_api import _check_key, get_operator_key

log = get_logger("scene_api")

scene_router = APIRouter(tags=["scene"])

_tdm104_client: Any = None


def get_scene_stdb() -> Any:
    """FastAPI dependency: a sync keep-alive SpacetimeDB client (``sql``, ``call_reducer``); tests override it."""
    global _tdm104_client
    if _tdm104_client is None:
        from rdf_common.settings import SpacetimeSettings, load_settings
        from rdf_common.spacetime_client import SpacetimeClient

        _tdm104_client = SpacetimeClient.from_settings(load_settings(SpacetimeSettings, "orchestrator"), timeout=20.0)
    return _tdm104_client


def get_scene_llm() -> Any:
    """The LLM client for generation (None = the shared ``rdf_common.llm`` switchable client)."""
    return None


def _store(stdb: Any) -> sc.SceneStore:
    return sc.SceneStore(stdb.sql, stdb.call_reducer)


async def _db(fn, *a, **kw):
    try:
        return await asyncio.to_thread(fn, *a, **kw)
    except (HTTPException, LookupError, ss.SceneSpecError, sc.SceneGenerationError):
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB unreachable: {str(e)[:300]}") from e


async def _ticket(stdb: Any, ticket_id: int) -> dict[str, Any]:
    rows = await _db(stdb.sql, f"SELECT * FROM ticket WHERE id = {int(ticket_id)}")
    if not rows:
        raise HTTPException(404, f"ticket {ticket_id} not found")
    return dict(rows[0])


class GenerateBody(BaseModel):
    hint: str | None = Field(None, max_length=1000, description="Extra guidance for Claude, e.g. 'use a green plate'.")


class PutBody(BaseModel):
    spec: dict[str, Any]


class DeployBody(BaseModel):
    revision: int | None = Field(None, ge=1, description="Default: the newest revision.")
    robot_id: int | None = Field(None, ge=1, description="Default: the ticket's robot if it is a sim robot, else the one online sim robot.")


class ValidateBody(BaseModel):
    spec: dict[str, Any]


@scene_router.get("/tickets/{ticket_id}/scene")
async def get_ticket_scene(ticket_id: int, revision: int | None = Query(None, ge=1), stdb: Any = Depends(get_scene_stdb)
                           ) -> dict[str, Any]:
    rows = await _db(_store(stdb).rows)
    out = sc.ticket_scene(rows, ticket_id, revision)
    if revision is not None and out["spec"] is None:
        raise HTTPException(404, f"ticket {ticket_id} has no scene revision {revision}")
    if out["spec"] is not None:
        out["success"] = ss.describe_success(out["spec"])
    return out


@scene_router.post("/tickets/{ticket_id}/scene/generate")
async def generate_ticket_scene(ticket_id: int, body: GenerateBody | None = None,
                                x_rdf_operator_key: str | None = Header(default=None),
                                x_human_id: str | None = Header(default=None),
                                operator_key: str | None = Depends(get_operator_key),
                                stdb: Any = Depends(get_scene_stdb), llm: Any = Depends(get_scene_llm)) -> dict[str, Any]:
    _check_key("generate_scene", x_rdf_operator_key, operator_key)
    t = await _ticket(stdb, ticket_id)
    task = str(t.get("task_description") or "").strip()
    if not task:
        raise HTTPException(400, f"ticket {ticket_id} has no task description")
    try:
        p = await _db(_store(stdb).generate, ticket_id, task, hint=(body.hint if body else None),
                      by=f"human:{(x_human_id or 'operator')[:64]}", llm=llm)
    except sc.SceneGenerationError as e:
        if e.errors:
            raise HTTPException(422, {"error": str(e), "errors": e.errors, "attempts": e.attempts}) from e
        raise HTTPException(502, str(e)) from e
    return _created(p)


@scene_router.put("/tickets/{ticket_id}/scene")
async def put_ticket_scene(ticket_id: int, body: PutBody, x_rdf_operator_key: str | None = Header(default=None),
                           x_human_id: str | None = Header(default=None),
                           operator_key: str | None = Depends(get_operator_key),
                           stdb: Any = Depends(get_scene_stdb)) -> dict[str, Any]:
    _check_key("put_scene", x_rdf_operator_key, operator_key)
    await _ticket(stdb, ticket_id)
    try:
        p = await _db(_store(stdb).create, ticket_id, body.spec, source="manual",
                      by=f"human:{(x_human_id or 'operator')[:64]}")
    except ss.SceneSpecError as e:
        raise HTTPException(422, {"error": "invalid scene spec", "errors": e.errors}) from e
    return _created(p)


def _created(p: dict[str, Any]) -> dict[str, Any]:
    return {"ticket_id": p["ticket_id"], "revision": p["revision"], "scene_id": p["scene_id"], "source": p["source"],
            "summary": p["summary"], "fixes": p.get("fixes", []), "spec": p["spec"],
            "success": ss.describe_success(p["spec"])}


@scene_router.post("/tickets/{ticket_id}/scene/deploy")
async def deploy_ticket_scene(ticket_id: int, body: DeployBody | None = None,
                              x_rdf_operator_key: str | None = Header(default=None),
                              x_human_id: str | None = Header(default=None),
                              operator_key: str | None = Depends(get_operator_key),
                              stdb: Any = Depends(get_scene_stdb)) -> dict[str, Any]:
    _check_key("deploy_scene", x_rdf_operator_key, operator_key)
    body = body or DeployBody()
    t = await _ticket(stdb, ticket_id)
    robots = await _db(stdb.sql, "SELECT * FROM robot")
    rid, why = sc.pick_sim_robot([dict(r) for r in robots], ticket_robot_id=int(t.get("robot_id") or 0) or None,
                                 requested=body.robot_id)
    if rid is None:
        raise HTTPException(409, why)
    try:
        p = await _db(_store(stdb).deploy, ticket_id, rid, revision=body.revision,
                      by=f"human:{(x_human_id or 'operator')[:64]}")
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    return {k: p[k] for k in ("ticket_id", "revision", "scene_id", "robot_id", "deploy_id", "by", "summary")} | {
        "status": "pending", "event": sc.EV_DEPLOYED}


@scene_router.get("/robots/{robot_id}/scene")
async def get_robot_scene(robot_id: int, stdb: Any = Depends(get_scene_stdb)) -> dict[str, Any]:
    rows = await _db(_store(stdb).rows)
    return sc.robot_scene(rows, robot_id)


@scene_router.post("/scenes/validate")
async def validate_scene(body: ValidateBody) -> dict[str, Any]:
    try:
        spec = ss.validate(body.spec)
    except ss.SceneSpecError as e:
        return {"ok": False, "errors": e.errors, "spec": None}
    return {"ok": True, "errors": [], "spec": spec, "success": ss.describe_success(spec),
            "fixes": spec.get("meta", {}).get("fixes", [])}


@scene_router.get("/scenes/default")
async def default_scene() -> dict[str, Any]:
    spec = ss.default_spec()
    return {"spec": spec, "success": ss.describe_success(spec)}


__all__ = ["get_scene_llm", "get_scene_stdb", "scene_router"]
