"""TDM-108 job-drop routes. The browser reaches them through the web app's proxy (``/api/tdm108/*``).

  GET  /drops                          status (mode, channels set/unset, hourly cap) + recent drops + attribution
                                        (operator key: post texts may carry the access code)
  GET  /drops/{drop_id}/card/{ch}.png  a drop's card for one channel                  (operator key)
  POST /drops/{drop_id}/post {channel} "post now": live mode only, one post per task per channel (operator key)
  POST /drops/preview {task_id}        build (or rebuild) the dry-run drop of an open task (operator key)
  GET  /drops/share/{task_id}.png      the marketplace "Share" card (?size=square|wide), src=share. Public:
                                        it shows what the marketplace already shows.
  POST /drops/claim-src                {task_id, src, identity, spacetime_token}: the claimant's browser says
                                        which link brought it; logged once per task as ``job_drop.claimed``
  GET  /drops/attribution              claims and posts per channel (operator key)

Reads come from the bus mirror (``app.state.bus``), so nothing here queries SpacetimeDB per request except
the identity check on claim-src (the same proof as TDM-65's /push/subscribe).
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import job_drop_card as card
from . import job_drops
from .log import get_logger
from .robot_commands_api import _check_key, get_operator_key
from .stream_api import IdentityVerifier, Tdm55Stdb, get_identity_verifier, get_stream_stdb

log = get_logger("job_drops_api")

job_drops_router = APIRouter(tags=["job-drops"])

Rows = Callable[[str], list]


def get_dropper() -> job_drops.Dropper:
    return job_drops.get_dropper()


def get_drop_rows(request: Request) -> Rows:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        return lambda table: []
    return lambda table: [dict(r) for r in bus.rows(table)]


def _op(key: str | None) -> None:
    _check_key("job_drop", key, get_operator_key())


class PostNow(BaseModel):
    channel: str = Field(pattern=r"^[a-z]{1,16}$")


class PreviewRequest(BaseModel):
    task_id: int = Field(ge=1)


class ClaimSrc(BaseModel):
    task_id: int = Field(ge=1)
    src: str = Field(pattern=r"^[a-z0-9_-]{1,24}$")
    identity: str
    spacetime_token: str = Field(min_length=10, max_length=8192, repr=False)


@job_drops_router.get("/drops")
def list_drops(d: job_drops.Dropper = Depends(get_dropper), rows: Rows = Depends(get_drop_rows),
               x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    _op(x_rdf_operator_key)
    return {**d.status(), "drops": [x.public() for x in d.store.recent(30)],
            "attribution": job_drops.attribution(rows("event_log"))}


@job_drops_router.get("/drops/attribution")
def drop_attribution(rows: Rows = Depends(get_drop_rows), x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    _op(x_rdf_operator_key)
    return job_drops.attribution(rows("event_log"))


@job_drops_router.get("/drops/share/{task_id}.png")
async def share_card(task_id: int, size: str = "square", d: job_drops.Dropper = Depends(get_dropper),
                     rows: Rows = Depends(get_drop_rows)) -> Response:
    if size not in card.SIZES:
        raise HTTPException(422, "size is square or wide")
    key = (task_id, size, job_drops.public_base(d.s), job_drops.access_code(d.s))
    hit = _share_cache.get(key)
    if hit and time.time() - hit[0] < 300:
        return Response(hit[1], media_type="image/png", headers={"cache-control": "public, max-age=300"})
    f, skipped = job_drops.facts_from_rows([task_id], rows, d.s.types)
    if f is None:
        raise HTTPException(404, f"task {task_id} is not open for sharing ({skipped.get(task_id, 'unknown')})")
    png = await asyncio.to_thread(_render_share, d, f, size)
    _share_cache[key] = (time.time(), png)
    while len(_share_cache) > 64:
        _share_cache.popitem(last=False)
    return Response(png, media_type="image/png", headers={"cache-control": "public, max-age=300"})


_share_cache: "OrderedDict[tuple, tuple[float, bytes]]" = OrderedDict()


def _render_share(d: job_drops.Dropper, f: job_drops.DropFacts, size: str) -> bytes:
    link = job_drops.task_link(job_drops.public_base(d.s), f.task_ids[0], "share", job_drops.access_code(d.s))
    frame, label, _ = d._frame(f)
    return card.render_card(d.card_info(f), size=size, link=link, short=job_drops._short(link), frame=frame,
                            frame_label=label)


@job_drops_router.get("/drops/{drop_id}/card/{channel}.png")
def drop_card(drop_id: str, channel: str, d: job_drops.Dropper = Depends(get_dropper),
              x_rdf_operator_key: str | None = Header(default=None)) -> FileResponse:
    _op(x_rdf_operator_key)
    if channel not in d.channels:
        raise HTTPException(404, "unknown channel")
    try:
        p = d.store.dir(drop_id) / f"card-{channel}.png"
    except ValueError:
        raise HTTPException(404, "unknown drop") from None
    if not p.exists():
        raise HTTPException(404, "no card")
    return FileResponse(p, media_type="image/png", headers={"cache-control": "no-store"})


@job_drops_router.post("/drops/{drop_id}/post")
async def post_now(drop_id: str, body: PostNow, d: job_drops.Dropper = Depends(get_dropper),
                   x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    _op(x_rdf_operator_key)
    try:
        d.store.dir(drop_id)
    except ValueError:
        raise HTTPException(404, "unknown drop") from None
    out = await asyncio.to_thread(d.post_now, drop_id, body.channel)
    if not out.get("ok") and out.get("code") in ("unknown_drop", "unknown_channel"):
        raise HTTPException(404, out["code"])
    if not out.get("ok") and out.get("code") in ("dry_run", "not_configured"):
        raise HTTPException(409, out.get("detail") or out["code"])
    return out


@job_drops_router.post("/drops/preview")
async def preview(body: PreviewRequest, d: job_drops.Dropper = Depends(get_dropper), rows: Rows = Depends(get_drop_rows),
                  x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    _op(x_rdf_operator_key)
    f, skipped = job_drops.facts_from_rows([body.task_id], rows, d.s.types)
    if f is None:
        raise HTTPException(409, f"task {body.task_id} is not droppable: {skipped.get(body.task_id)}")
    drop = await asyncio.to_thread(d.build, f)
    return drop.public()


@job_drops_router.post("/drops/claim-src")
async def claim_src(body: ClaimSrc, request: Request, d: job_drops.Dropper = Depends(get_dropper),
                    rows: Rows = Depends(get_drop_rows), stdb: Tdm55Stdb = Depends(get_stream_stdb),
                    verify: IdentityVerifier = Depends(get_identity_verifier)) -> dict:
    from .paging_api import _contributor_of

    me = await _contributor_of(body, stdb, verify)  # 401/404 unless the browser proves its contributor row
    cid = int(me.get("id") or 0)
    for _ in range(15):  # the claim may land in the mirror a moment after the browser's reducer returned
        task = next((t for t in rows("task") if int(t.get("id") or 0) == body.task_id), None)
        if task and int(task.get("assignee_id") or 0) == cid:
            return await asyncio.to_thread(d.record_claim, body.task_id, cid, body.src)
        await asyncio.sleep(0.2)
    raise HTTPException(409, "this task is not claimed by you")
