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


async def _contributor_id(body: _Tdm113Caller, stdb: Tdm55Stdb, verify: IdentityVerifier) -> int:
    from .paging_api import _contributor_of

    c = await _contributor_of(body, stdb, verify)  # 401 / 404 unless the browser proves its contributor row
    if (c.get("kind") or "contributor") != "contributor":
        raise HTTPException(403, "Dataset sellers don't get job texts.")
    return int(c["id"])


@text_offers_router.post("/texts/optin")
async def texts_optin(body: Tdm113OptIn, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                      verify: IdentityVerifier = Depends(get_identity_verifier),
                      t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    if not body.consent:
        raise HTTPException(422, "Tick the consent box to get job texts.")
    cid = await _contributor_id(body, stdb, verify)
    out = await asyncio.to_thread(t.opt_in, cid, body.phone, body.in_person, body.consent_text)
    if not out.get("ok"):
        raise HTTPException(422, out.get("detail") or out.get("code"))
    log.info("text.opted_in", extra={"fields": {"contributor_id": cid, "welcome": out["welcome"]["state"]}})
    return out


@text_offers_router.post("/texts/state")
async def texts_state(body: _Tdm113Caller, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                      verify: IdentityVerifier = Depends(get_identity_verifier),
                      t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    return t.state_for(await _contributor_id(body, stdb, verify))


@text_offers_router.post("/texts/resend")
async def texts_resend(body: _Tdm113Caller, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                       verify: IdentityVerifier = Depends(get_identity_verifier),
                       t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    cid = await _contributor_id(body, stdb, verify)
    out = await asyncio.to_thread(t.resend_welcome, cid)
    if not out.get("ok"):
        raise HTTPException(429 if out.get("code") == "rate_limited" else 409, out.get("detail") or out.get("code"))
    return out


@text_offers_router.post("/texts/optout")
async def texts_optout(body: _Tdm113Caller, stdb: Tdm55Stdb = Depends(get_stream_stdb),
                       verify: IdentityVerifier = Depends(get_identity_verifier),
                       t: text_offers.TextOffers = Depends(get_text_offers)) -> dict:
    return await asyncio.to_thread(t.opt_out, await _contributor_id(body, stdb, verify))


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
