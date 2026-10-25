"""TDM-113 text job offer routes. The browser reaches the contributor ones through the web app's proxy
(``/api/tdm113/*``); they prove the caller the same way as TDM-65's /push/* (the browser's own SpacetimeDB
identity token, verified by SpacetimeDB, bound to its ``contributor`` row). Phone numbers are never logged
or returned (only the last 4 digits).

  POST /texts/optin    {identity, spacetime_token, phone, in_person, consent, consent_text} -> opt in + welcome text
  POST /texts/state    {identity, spacetime_token} -> {opted_in, last4, in_person, mode, line}
  POST /texts/resend   {identity, spacetime_token} -> resend the welcome (after texting the line first)
  POST /texts/optout   {identity, spacetime_token}
  GET  /texts/status   mode, counts, Inkbox setup, recent (masked) outbox      (operator key)
  POST /texts/webhook  an Inkbox webhook (signed; the same handler the in-process tunnel uses)

Two devices, one person (laptop + phone):
  POST /texts/release     {identity, spacetime_token, task_id} -> "Give back control": release_task as the
                          orchestrator when the caller holds the task (or is the holder's merged account), and the
                          text-offer hold for it stops
  POST /texts/takeover    {identity, spacetime_token, task_id} -> the same person's second device opened the job link:
                          the claim moves to it (new claim generation) instead of being refused
  POST /texts/login_code  {phone} -> a 6-digit code by text to an opted-in number (same answer for unknown numbers)
  POST /texts/login       {identity, spacetime_token, phone, code} -> {token}: this browser adopts the SpacetimeDB
                          token of the contributor with that number (seen on their earlier verified call), so both
                          devices are the same contributor. Codes expire after 10 min, 5 tries, rate-limited.
Every verified call remembers (identity, contributor, token) in the private text store for that hand-off.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import text_offers
from .log import get_logger
from .robot_commands_api import _check_key, get_operator_key
from .stream_api import IdentityVerifier, Tdm55Stdb, get_identity_verifier, get_stream_stdb

log = get_logger("text_offers_api")

text_offers_router = APIRouter(tags=["text-offers"])


def get_text_offers() -> text_offers.TextOffers:
    return text_offers.get_offers()


class _Tdm113Caller(BaseModel):
    identity: str
    spacetime_token: str = Field(min_length=10, max_length=8192, repr=False)


class Tdm113OptIn(_Tdm113Caller):
    phone: str = Field(min_length=7, max_length=32, repr=False)
    in_person: bool = False
    consent: bool
    consent_text: str = Field(max_length=300)


class Tdm113Task(_Tdm113Caller):
    task_id: int = Field(ge=1)


class Tdm113LoginCode(BaseModel):
    phone: str = Field(min_length=7, max_length=32, repr=False)


class Tdm113Login(_Tdm113Caller):
    phone: str = Field(min_length=7, max_length=32, repr=False)
    code: str = Field(min_length=6, max_length=8, repr=False)


def _remember(t: text_offers.TextOffers | None, body: _Tdm113Caller, cid: int) -> None:
    if t is None:
        return
    from .paging_api import _norm_identity

    try:
        t.store.remember_device(_norm_identity(body.identity), cid, body.spacetime_token, t.clock())
    except Exception as e:  # noqa: BLE001 - a convenience for phone sign-in, never a reason to fail the call
        log.warning("text.remember_device_failed", extra={"fields": {"error": type(e).__name__}})


async def _contributor_id(body: _Tdm113Caller, stdb: Tdm55Stdb, verify: IdentityVerifier,
                          t: text_offers.TextOffers | None = None) -> int:
    from .paging_api import _contributor_of

    c = await _contributor_of(body, stdb, verify)  # 401 / 404 unless the browser proves its contributor row
    if (c.get("kind") or "contributor") != "contributor":
        raise HTTPException(403, "Dataset sellers don't get job texts." if c.get("kind") == "dataset_seller"
                            else "This account was merged into another one. Sign in with your phone number.")
    _remember(t, body, int(c["id"]))
    return int(c["id"])


async def _task_row(stdb: Tdm55Stdb, task_id: int) -> dict | None:
    try:
        rows = await stdb.sql(f"SELECT * FROM task WHERE id = {int(task_id)}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}") from None
    return rows[0] if rows else None


@text_offers_router.post("/texts/optin")
async def texts_optin(body: Tdm113OptIn, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                      verify: IdentityVerifier = Depends(get_identity_verifier),
                      t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    if not body.consent:
        raise HTTPException(422, "Tick the consent box to get job texts.")
    cid = await _contributor_id(body, stdb, verify, t)
    out = await asyncio.to_thread(t.opt_in, cid, body.phone, body.in_person, body.consent_text)
    if not out.get("ok"):
        raise HTTPException(422, out.get("detail") or out.get("code"))
    log.info("text.opted_in", extra={"fields": {"contributor_id": cid, "welcome": out["welcome"]["state"]}})
    return out


@text_offers_router.post("/texts/state")
async def texts_state(body: _Tdm113Caller, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                      verify: IdentityVerifier = Depends(get_identity_verifier),
                      t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    return t.state_for(await _contributor_id(body, stdb, verify, t))


@text_offers_router.post("/texts/resend")
async def texts_resend(body: _Tdm113Caller, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                       verify: IdentityVerifier = Depends(get_identity_verifier),
                       t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    cid = await _contributor_id(body, stdb, verify, t)
    out = await asyncio.to_thread(t.resend_welcome, cid)
    if not out.get("ok"):
        raise HTTPException(429 if out.get("code") == "rate_limited" else 409, out.get("detail") or out.get("code"))
    return out


@text_offers_router.post("/texts/optout")
async def texts_optout(body: _Tdm113Caller, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                       verify: IdentityVerifier = Depends(get_identity_verifier),
                       t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    return await asyncio.to_thread(t.opt_out, await _contributor_id(body, stdb, verify))


_STATUS = {"no_task": 404, "not_holder": 403, "taken": 409, "closed": 409, "bad_phone": 422, "bad_code": 401,
           "rate_limited": 429, "no_device": 409}


def _fail(out: dict) -> HTTPException:
    return HTTPException(_STATUS.get(out.get("code") or "", 400), out.get("detail") or out.get("code"))


@text_offers_router.post("/texts/release")
async def texts_release(body: Tdm113Task, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                        verify: IdentityVerifier = Depends(get_identity_verifier),
                        t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    cid = await _contributor_id(body, stdb, verify, t)
    task = await _task_row(stdb, body.task_id)
    try:
        out = await asyncio.to_thread(t.give_back, cid, task)
    except Exception as e:  # noqa: BLE001 - the reducer refused
        raise HTTPException(409, f"release_task failed: {str(e)[:200]}") from None
    if not out.get("ok"):
        raise _fail(out)
    log.info("text.gave_back", extra={"fields": {"contributor_id": cid, "task_id": body.task_id,
                                                 "released": out.get("released"), "hold": out.get("hold_dropped")}})
    return out


@text_offers_router.post("/texts/takeover")
async def texts_takeover(body: Tdm113Task, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                         verify: IdentityVerifier = Depends(get_identity_verifier),
                         t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    cid = await _contributor_id(body, stdb, verify, t)
    task = await _task_row(stdb, body.task_id)
    try:
        out = await asyncio.to_thread(t.take_over, cid, task)
    except Exception as e:  # noqa: BLE001 - claim_task refused (someone else won the race)
        raise HTTPException(409, f"Could not take over: {str(e)[:200]}") from None
    if not out.get("ok"):
        raise _fail(out)
    log.info("text.took_over", extra={"fields": {"contributor_id": cid, "task_id": body.task_id,
                                                 "previous_holder_id": out.get("previous_holder_id")}})
    return out


@text_offers_router.post("/texts/login_code")
async def texts_login_code(body: Tdm113LoginCode, t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    out = await asyncio.to_thread(t.login_start, body.phone)
    if not out.get("ok"):
        raise _fail(out)
    return out


@text_offers_router.post("/texts/login")
async def texts_login(body: Tdm113Login, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                      verify: IdentityVerifier = Depends(get_identity_verifier),
                      t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    from .paging_api import _HEX, _norm_identity

    if not _HEX.match(body.identity):
        raise HTTPException(422, "identity must be 64 hex characters")
    me = _norm_identity(body.identity)
    if not await verify(me, body.spacetime_token):
        raise HTTPException(401, "SpacetimeDB token does not belong to this identity")
    out = await asyncio.to_thread(t.login_check, body.phone, body.code)
    if not out.get("ok"):
        raise _fail(out)
    cid = int(out["contributor_id"])
    try:
        rows = await stdb.sql("SELECT * FROM contributor")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SpacetimeDB request failed: {type(e).__name__}") from None
    target = next((r for r in rows if int(r.get("id") or 0) == cid), None)
    if target is None:
        raise HTTPException(404, "That number's account is gone. Sign up again at /join.")
    target_identity = _norm_identity(target.get("identity"))
    if target_identity == me:
        _remember(t, body, cid)
        return {"ok": True, "contributor_id": cid, "switch": False, "name": target.get("name")}
    for ident, token in t.store.devices_for(cid):
        if ident != target_identity:  # a device remembered before a merge moved the identity: no longer it
            continue
        if await verify(ident, token):
            log.info("text.login", extra={"fields": {"contributor_id": cid, "switch": True}})
            return {"ok": True, "contributor_id": cid, "switch": True, "token": token, "name": target.get("name")}
        t.store.forget_device(ident)
    log.info("text.login_no_device", extra={"fields": {"contributor_id": cid}})
    raise HTTPException(409, "Signed in, but we can't move this browser yet: open GeonosisAI once on the device you "
                             "signed up with (the Join page), then enter a new code here.")


@text_offers_router.get("/texts/status")
def texts_status(t: text_offers.TextOffers = Depends(get_text_offers),
                 x_rdf_operator_key: str | None = Header(default=None)) -> dict:
    _check_key("text_offers", x_rdf_operator_key, get_operator_key())
    return t.status()


@text_offers_router.post("/texts/webhook")
async def texts_webhook(request: Request, t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    raw = await request.body()
    if len(raw) > 256_000:
        raise HTTPException(413, "body too large")
    code, out = await asyncio.to_thread(t.handle_webhook, raw, dict(request.headers))
    if code != 200:
        raise HTTPException(code, out.get("reason") or "rejected")
    return out
