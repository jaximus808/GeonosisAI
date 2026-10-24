"""TDM-65 web-push routes. The browser reaches them through the web app's proxy (``/api/tdm65/*``),
because the orchestrator's CORS is GET-only.

  GET  /push/config        {enabled, public_key, rate_limit_s}            (public key only)
  GET  /push/status        {enabled, subscriptions, contributors, recent} (counts only, no endpoints)
  POST /push/subscribe     {identity, spacetime_token, subscription, user_agent?} -> store it
  POST /push/unsubscribe   {identity, spacetime_token, endpoint}
  POST /push/state         {identity, spacetime_token, endpoint} -> {subscribed}  (is this device on?)
  POST /push/test          {identity, spacetime_token} -> one test push to the caller's devices

Writes prove who is calling the same way as TDM-55's /stream/token: the browser sends its own
SpacetimeDB identity token, SpacetimeDB verifies it (``/v1/identity/{id}/verify``), and the
subscription is bound to the ``contributor`` row with that identity (kind ``contributor`` only).
Tokens and endpoints are never logged.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import paging
from .log import get_logger
from .stream_api import IdentityVerifier, Tdm55Stdb, _norm_identity, get_identity_verifier, get_stream_stdb

log = get_logger("paging_api")

paging_router = APIRouter(tags=["push"])

_HEX = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_B64URL = re.compile(r"^[A-Za-z0-9_\-=]{8,200}$")


class _Caller(BaseModel):
    identity: str = Field(description="this browser's SpacetimeDB identity, hex")
    spacetime_token: str = Field(min_length=10, max_length=8192, repr=False)


class _Keys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=200)


class _Sub(BaseModel):
    endpoint: str = Field(max_length=2048, repr=False)
    keys: _Keys = Field(repr=False)


class SubscribeRequest(_Caller):
    subscription: _Sub = Field(repr=False)
    user_agent: str = Field(default="", max_length=400)


class EndpointRequest(_Caller):
    endpoint: str = Field(max_length=2048, repr=False)


def get_pager() -> paging.Pager:
    return paging.get_pager()


async def _contributor_of(body: _Caller, stdb: Tdm55Stdb, verify: IdentityVerifier) -> dict:
    if not _HEX.match(body.identity):
        raise HTTPException(422, "identity must be 64 hex characters")
    identity = _norm_identity(body.identity)
    if not await verify(identity, body.spacetime_token):
        raise HTTPException(401, "SpacetimeDB token does not belong to this identity")
    try:
        rows = await stdb.sql("SELECT * FROM contributor")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}") from None
    mine = [r for r in rows if _norm_identity(r.get("identity")) == identity]
    if not mine:
        raise HTTPException(404, "This browser has not joined yet. Sign up at /join first.")
    return mine[0]


@paging_router.get("/push/config")
def push_config(pager: paging.Pager = Depends(get_pager)) -> dict:
    return {"enabled": pager.enabled, "public_key": pager.public_key, "rate_limit_s": pager.rate_s}


@paging_router.get("/push/status")
def push_status(pager: paging.Pager = Depends(get_pager)) -> dict:
    return pager.status()


@paging_router.post("/push/subscribe")
async def push_subscribe(body: SubscribeRequest, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                         verify: IdentityVerifier = Depends(get_identity_verifier),
                         pager: paging.Pager = Depends(get_pager)) -> dict:
    if not pager.enabled:
        raise HTTPException(503, "Push is not configured on the orchestrator (VAPID keys missing).")
    sub = body.subscription
    if not paging.endpoint_allowed(sub.endpoint):
        raise HTTPException(422, "Not a known browser push service endpoint.")
    if not (_B64URL.match(sub.keys.p256dh) and _B64URL.match(sub.keys.auth)):
        raise HTTPException(422, "Subscription keys are not base64url.")
    c = await _contributor_of(body, stdb, verify)
    if (c.get("kind") or "contributor") != "contributor":
        raise HTTPException(403, "Dataset sellers don't get task notifications.")
    cid = int(c["id"])
    pager.store.upsert(paging.Subscription(sub.endpoint, cid, sub.keys.p256dh, sub.keys.auth, body.user_agent))
    n = len(pager.store.for_contributor(cid))
    log.info("paging.subscribed", extra={"fields": {"contributor_id": cid, "devices": n}})
    return {"ok": True, "contributor_id": cid, "devices": n}


@paging_router.post("/push/unsubscribe")
async def push_unsubscribe(body: EndpointRequest, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                           verify: IdentityVerifier = Depends(get_identity_verifier),
                           pager: paging.Pager = Depends(get_pager)) -> dict:
    c = await _contributor_of(body, stdb, verify)
    removed = pager.store.delete(body.endpoint, int(c["id"]))
    log.info("paging.unsubscribed", extra={"fields": {"contributor_id": int(c["id"]), "removed": removed}})
    return {"ok": True, "removed": removed}


@paging_router.post("/push/state")
async def push_state(body: EndpointRequest, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                     verify: IdentityVerifier = Depends(get_identity_verifier),
                     pager: paging.Pager = Depends(get_pager)) -> dict:
    c = await _contributor_of(body, stdb, verify)
    return {"subscribed": pager.store.has(body.endpoint, int(c["id"])), "enabled": pager.enabled}


@paging_router.post("/push/test")
async def push_test(body: _Caller, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                    verify: IdentityVerifier = Depends(get_identity_verifier),
                    pager: paging.Pager = Depends(get_pager)) -> dict:
    c = await _contributor_of(body, stdb, verify)
    import asyncio

    out = await asyncio.to_thread(pager.test, int(c["id"]))
    log.info("paging.test", extra={"fields": {"contributor_id": int(c["id"]), **out}})
    return out
