"""HTTP surface for episode media (TDM-4).

Mount ``media_router`` in the orchestrator's main FastAPI app, or run this file
standalone for local dev:

    cd services/orchestrator && uv run uvicorn rdf_orchestrator.media_api:app --port 8000

Routes:
  POST /uploads/sign        {ticket_id, episode_id, filename, content_type?, ttl_s?}
                            -> {upload_url, method, headers, storage_uri, key, expires_at}
  POST /uploads/sign-batch  {ticket_id, episode_id, files:[{filename, content_type?}], ttl_s?}
                            -> {uploads:[...as above...]}
  GET  /media/sign?uri=     -> {url, storage_uri, content_type, expires_at}   (dashboard <video src>)
  GET  /media/list?ticket_id=&episode_id=   -> {objects:[{key, storage_uri, size, mtime}]}
  GET  /media/healthz
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from rdf_orchestrator import storage

media_router = APIRouter(tags=["media"])


class SignUploadRequest(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=128)
    episode_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255, description="e.g. cam0.mp4, actions.parquet, thumb.jpg")
    content_type: str | None = Field(default=None, max_length=128)
    ttl_s: int = Field(default=storage.DEFAULT_PUT_TTL_S, ge=1, le=storage.MAX_TTL_S)


class SignedUpload(BaseModel):
    upload_url: str
    method: str
    headers: dict[str, str]
    storage_uri: str
    key: str
    expires_at: int


class BatchFile(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str | None = Field(default=None, max_length=128)


class SignUploadBatchRequest(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=128)
    episode_id: str = Field(min_length=1, max_length=128)
    files: list[BatchFile] = Field(min_length=1, max_length=64)
    ttl_s: int = Field(default=storage.DEFAULT_PUT_TTL_S, ge=1, le=storage.MAX_TTL_S)


class SignedMedia(BaseModel):
    url: str
    storage_uri: str
    content_type: str | None
    expires_at: int


class ObjectOut(BaseModel):
    key: str
    storage_uri: str
    size: int
    mtime: int


def _sign_one(ticket_id: str, episode_id: str, filename: str, content_type: str | None, ttl_s: int) -> SignedUpload:
    try:
        key = storage.episode_key(ticket_id, episode_id, filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    signed = storage.get_backend().signed_put(key, content_type or storage.guess_content_type(key), ttl_s=ttl_s)
    return SignedUpload(upload_url=signed.url, method=signed.method, headers=signed.headers,
                        storage_uri=signed.storage_uri, key=signed.key, expires_at=signed.expires_at)


@media_router.post("/uploads/sign", response_model=SignedUpload)
def sign_upload(req: SignUploadRequest) -> SignedUpload:
    return _sign_one(req.ticket_id, req.episode_id, req.filename, req.content_type, req.ttl_s)


@media_router.post("/uploads/sign-batch")
def sign_upload_batch(req: SignUploadBatchRequest) -> dict[str, list[SignedUpload]]:
    return {"uploads": [_sign_one(req.ticket_id, req.episode_id, f.filename, f.content_type, req.ttl_s) for f in req.files]}


@media_router.get("/media/sign", response_model=SignedMedia)
def sign_media(uri: str = Query(min_length=1, max_length=2048),
               ttl_s: int = Query(storage.DEFAULT_GET_TTL_S, ge=1, le=storage.MAX_TTL_S)) -> SignedMedia:
    try:
        signed = storage.backend_for_uri(uri).signed_get(uri, ttl_s=ttl_s)
    except (ValueError, storage.StorageError) as e:
        raise HTTPException(400, str(e)) from None
    return SignedMedia(url=signed.url, storage_uri=signed.storage_uri, content_type=signed.content_type,
                       expires_at=signed.expires_at)


@media_router.get("/media/list")
def list_media(ticket_id: str = Query(min_length=1, max_length=128),
               episode_id: str | None = Query(default=None, max_length=128),
               limit: int = Query(1000, ge=1, le=10000)) -> dict[str, list[ObjectOut]]:
    prefix = f"tickets/{ticket_id}/" + (f"episodes/{episode_id}/" if episode_id else "")
    try:
        storage.validate_key(prefix.rstrip("/"))
        objs = storage.get_backend().list(prefix, limit=limit)
    except (ValueError, storage.StorageError) as e:
        raise HTTPException(400, str(e)) from None
    return {"objects": [ObjectOut(key=o.key, storage_uri=o.storage_uri, size=o.size, mtime=o.mtime) for o in objs]}


@media_router.get("/media/healthz")
def healthz() -> dict[str, object]:
    b = storage.get_backend()
    return {"ok": True, "media_backend": b.scheme, "bucket": b.bucket}


def create_app() -> FastAPI:
    api = FastAPI(title="rdf-orchestrator media API")
    api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    api.include_router(media_router)
    return api


app = create_app()
