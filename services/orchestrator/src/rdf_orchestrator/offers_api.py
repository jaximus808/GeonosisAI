"""HTTP surface for dataset-purchase offers (TDM-91). Mounted from ``app.py``; logic in ``offers.py``.

  POST /tickets/{ticket_id}/offers     {price_per_episode_cents? | from_plan: true, seller_id?, source_uri?,
                                        accept?}  -> 201 {offer, needed_count (= N - existing), max_cost_cents, ...}
  GET  /tickets/{ticket_id}/offers     -> {offers: [...]}
  GET  /offers/{offer_id}              -> offer + per-episode verdicts (qa_result + review) + payments
  POST /offers/{offer_id}/accept       seller accepts (the seller may also call accept_dataset_offer itself)
  POST /offers/{offer_id}/uploads/sign {episodes: [{ref, files: [{filename, content_type?}]}]}
                                       | {lerobot_files: ["meta/info.json", "data/chunk-000/episode_000000.parquet", ...]}
                                       -> signed PUT URLs under tickets/{t}/purchased/{offer}/; only for open slots
  POST /offers/{offer_id}/episodes     upload manifest -> ingest_purchased_episode per episode
                                       -> {ingested, already_ingested, refused: [{ref, code, reason}], ...}
  POST /offers/{offer_id}/delivered    the seller has sent everything (lets a part-accepted offer settle)
  POST /offers/{offer_id}/finalize     finalize_offer + reserve_dataset_payment(offer, "settle") now
  POST /offers/{offer_id}/cancel       {reason}
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .offers import OfferError, OffersService

offers_router = APIRouter(tags=["offers"])


class CreateOfferRequest(BaseModel):
    price_per_episode_cents: int | None = Field(None, gt=0, description="Required unless from_plan.")
    from_plan: bool = Field(False, description="Use the latest plan's consider_dataset_offer / offer_max_price_cents.")
    seller_id: int | None = Field(None, ge=1, description="contributor.id (kind dataset_seller); default: first registered seller.")
    source_uri: str | None = Field(None, max_length=2048, description="Where the seller's dataset lives (e.g. hf://org/ds).")
    accept: bool = Field(False, description="Accept on the seller's behalf right away.")


class FileSpec(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str | None = Field(None, max_length=128)


class EpisodeFiles(BaseModel):
    ref: str = Field(min_length=1, max_length=128, description="Seller's episode name, e.g. ep_0003.")
    files: list[FileSpec] = Field(min_length=1, max_length=16)


class SignRequest(BaseModel):
    episodes: list[EpisodeFiles] = Field(default_factory=list, max_length=500)
    lerobot_files: list[str] = Field(default_factory=list, max_length=5000)
    ttl_s: int | None = Field(None, ge=60, le=7 * 24 * 3600)


class RawEpisode(BaseModel):
    ref: str = Field(min_length=1, max_length=128)
    cameras: dict[str, str] = Field(description="{camera: filename under the episode folder, or its storage uri}")
    actions: str = Field("actions.parquet", description="filename or storage uri of the actions parquet")
    frame_count: int = Field(gt=0)
    duration_ms: int = Field(gt=0)
    success: bool | None = None


class IngestRequest(BaseModel):
    format: Literal["raw", "lerobot"] = "raw"
    episodes: list[dict[str, Any]] = Field(default_factory=list, max_length=1000,
                                           description="raw: RawEpisode objects; lerobot: optional episodes.jsonl rows")
    info: dict[str, Any] | None = Field(None, description="lerobot: meta/info.json (read from storage when omitted)")
    final: bool = Field(False, description="This is the seller's last delivery for the offer.")
    verify: bool = Field(True, description="Check every file exists in storage before ingesting.")


class CancelRequest(BaseModel):
    reason: str = Field("", max_length=500)


def _default_service() -> OffersService:
    from rdf_common.settings import OrchestratorSettings, load_settings
    from rdf_common.spacetime_client import AsyncSpacetimeClient

    return OffersService(AsyncSpacetimeClient.from_settings(load_settings(OrchestratorSettings, "orchestrator")))


def get_offers_service(request: Request) -> OffersService:
    svc = getattr(request.app.state, "offers_service", None)
    if svc is None:
        svc = request.app.state.offers_service = _default_service()
    return svc


async def _run(coro):
    try:
        return await coro
    except OfferError as e:
        raise e.http() from None


@offers_router.post("/tickets/{ticket_id}/offers", status_code=201)
async def create_offer(ticket_id: int, body: CreateOfferRequest,
                       svc: OffersService = Depends(get_offers_service)) -> dict:
    price = body.price_per_episode_cents
    if body.from_plan:
        hint = await _run(svc.plan_hint(ticket_id))
        if not hint or not hint.get("consider"):
            raise HTTPException(422, {"code": "plan_has_no_offer",
                                      "message": "The latest data plan does not suggest a dataset offer.", "hint": hint})
        price = price or int(hint.get("max_price_per_episode_cents") or 0)
    if not price:
        raise HTTPException(422, {"code": "price_required", "message": "Pass price_per_episode_cents or from_plan."})
    return await _run(svc.create_offer(ticket_id, price, seller_id=body.seller_id, source_uri=body.source_uri,
                                       accept=body.accept, origin="plan" if body.from_plan else "manual"))


@offers_router.get("/tickets/{ticket_id}/offers")
async def list_offers(ticket_id: int, svc: OffersService = Depends(get_offers_service)) -> dict:
    return {"offers": await _run(svc.list_for_ticket(ticket_id))}


@offers_router.get("/offers/{offer_id}")
async def get_offer(offer_id: int, svc: OffersService = Depends(get_offers_service)) -> dict:
    return await _run(svc.summary(offer_id))


@offers_router.post("/offers/{offer_id}/accept")
async def accept_offer(offer_id: int, svc: OffersService = Depends(get_offers_service)) -> dict:
    return await _run(svc.accept(offer_id))


@offers_router.post("/offers/{offer_id}/uploads/sign")
async def sign_uploads(offer_id: int, body: SignRequest, svc: OffersService = Depends(get_offers_service)) -> dict:
    if not body.episodes and not body.lerobot_files:
        raise HTTPException(422, {"code": "nothing_to_sign", "message": "Pass episodes or lerobot_files."})
    return await _run(svc.sign_uploads(offer_id, [e.model_dump() for e in body.episodes], body.lerobot_files,
                                       body.ttl_s))


@offers_router.post("/offers/{offer_id}/episodes")
async def ingest_episodes(offer_id: int, body: IngestRequest, svc: OffersService = Depends(get_offers_service)) -> dict:
    manifest = body.model_dump()
    if body.format == "raw":
        if not body.episodes:
            raise HTTPException(422, {"code": "no_episodes", "message": "raw format needs episodes."})
        try:
            manifest["episodes"] = [RawEpisode(**e).model_dump() for e in body.episodes]
        except Exception as e:  # pydantic ValidationError -> 422 with its text
            raise HTTPException(422, {"code": "bad_episode", "message": str(e)}) from None
    elif not body.episodes:
        manifest["episodes"] = None  # read meta/episodes.jsonl from storage
    return await _run(svc.ingest(offer_id, manifest))


@offers_router.post("/offers/{offer_id}/delivered")
async def delivered(offer_id: int, svc: OffersService = Depends(get_offers_service)) -> dict:
    await _run(svc.mark_delivery_complete(offer_id))
    return await _run(svc.summary(offer_id))


@offers_router.post("/offers/{offer_id}/finalize")
async def finalize(offer_id: int, svc: OffersService = Depends(get_offers_service)) -> dict:
    return await _run(svc.finalize(offer_id))


@offers_router.post("/offers/{offer_id}/cancel")
async def cancel(offer_id: int, body: CancelRequest, svc: OffersService = Depends(get_offers_service)) -> dict:
    return await _run(svc.cancel(offer_id, body.reason))
