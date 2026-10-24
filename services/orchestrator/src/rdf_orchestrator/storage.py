"""Episode media storage interface (TDM-4).

Only ``storage_uri`` strings go into SpacetimeDB; the bytes live in a blob
store behind this interface. Callers never touch a backend directly:

    from rdf_orchestrator.storage import get_backend, episode_key

    store = get_backend()
    key = episode_key(ticket_id, episode_id, "cam0.mp4")
    signed = store.signed_put(key, "video/mp4")      # hand to Jetson / browser
    ...                                              # they PUT the bytes
    url = store.signed_get(signed.storage_uri).url   # dashboard <video src>
    store.list(f"tickets/{ticket_id}/")              # what exists

``storage_uri`` is always ``<scheme>://<bucket>/<key>`` with the key layout
``tickets/{ticket_id}/episodes/{episode_id}/{filename}``. ``signed_get``
dispatches on the scheme, so URIs written by one backend keep resolving after a
swap.

Backends:
  * ``modal``  (default; proven) - Modal Volume ``rdf-media`` behind the signed
    endpoint in ``services/media-store/``. URIs: ``modal-vol://rdf-media/<key>``.
    Modal training jobs mount the Volume and read ``/media/<key>`` directly.
  * ``s3``     (Tigris / MinIO / R2; boto3 presign; not exercised tonight) -
    URIs: ``s3://<bucket>/<key>``. Enable with ``RDF_MEDIA_BACKEND=s3`` plus
    ``STORAGE_ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY``.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote, urlsplit

from pydantic import Field, SecretStr
from rdf_common.settings import NonEmptyStr, RdfSettings, load_settings

mimetypes.add_type("application/vnd.apache.parquet", ".parquet")
mimetypes.add_type("video/mp4", ".mp4")

DEFAULT_PUT_TTL_S = 15 * 60
DEFAULT_GET_TTL_S = 60 * 60
MAX_TTL_S = 7 * 24 * 3600

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,254}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(/[A-Za-z0-9][A-Za-z0-9._\-]*)*$")


# ---------- settings ----------


class MediaStoreSettings(RdfSettings):
    """Env for the media store. Names match ``.env.example``."""

    rdf_media_backend: Literal["modal", "s3"] = "modal"
    # modal backend
    rdf_media_store_url: NonEmptyStr = "https://jaxonp808--rdf-media.modal.run"
    rdf_media_signing_key: SecretStr = Field(min_length=16)
    rdf_media_bucket: NonEmptyStr = "rdf-media"  # Volume name; the "bucket" in modal-vol:// URIs
    # s3 backend (Tigris on Fly, MinIO locally); required only when backend=s3
    storage_endpoint: str | None = None
    storage_bucket: str | None = None
    storage_access_key: SecretStr | None = None
    storage_secret_key: SecretStr | None = None
    storage_region: str = "auto"


# ---------- value types ----------


@dataclass(frozen=True)
class SignedUrl:
    url: str
    method: str  # "PUT" or "GET"
    storage_uri: str
    key: str
    expires_at: int  # unix seconds
    headers: dict[str, str] = field(default_factory=dict)  # headers the client MUST send (PUT)
    content_type: str | None = None


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    storage_uri: str
    size: int
    mtime: int


class StorageError(RuntimeError):
    pass


class StorageBackend(Protocol):
    scheme: str
    bucket: str

    def signed_put(self, key: str, content_type: str, *, ttl_s: int = DEFAULT_PUT_TTL_S) -> SignedUrl: ...
    def signed_get(self, uri_or_key: str, *, ttl_s: int = DEFAULT_GET_TTL_S) -> SignedUrl: ...
    def put(self, key: str, data: bytes | Path, content_type: str | None = None) -> str: ...
    def list(self, prefix: str = "", *, limit: int = 1000) -> list[ObjectInfo]: ...


# ---------- keys and URIs ----------


def episode_key(ticket_id: str, episode_id: str, filename: str) -> str:
    """``tickets/{ticket_id}/episodes/{episode_id}/{filename}`` with validation."""
    for name, val, rx in (("ticket_id", ticket_id, _ID_RE), ("episode_id", episode_id, _ID_RE), ("filename", filename, _FILENAME_RE)):
        if not rx.match(val or ""):
            raise ValueError(f"invalid {name}: only [A-Za-z0-9._-], no slashes")
    return f"tickets/{ticket_id}/episodes/{episode_id}/{filename}"


def validate_key(key: str) -> str:
    if not key or len(key) > 1024 or not _KEY_RE.match(key) or ".." in key.split("/"):
        raise ValueError("invalid storage key")
    return key


def make_uri(scheme: str, bucket: str, key: str) -> str:
    return f"{scheme}://{bucket}/{validate_key(key)}"


def parse_uri(uri: str) -> tuple[str, str, str]:
    """``<scheme>://<bucket>/<key>`` -> (scheme, bucket, key)."""
    parts = urlsplit(uri)
    key = parts.path.lstrip("/")
    if not parts.scheme or not parts.netloc or not key:
        raise ValueError(f"not a storage uri: {uri!r}")
    return parts.scheme, parts.netloc, validate_key(key)


def guess_content_type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


def _ttl(ttl_s: int) -> int:
    return max(1, min(int(ttl_s), MAX_TTL_S))


# ---------- modal volume backend ----------


def _signature(key_secret: str, method: str, key: str, exp: int) -> str:
    """Keep byte-for-byte identical to services/media-store/rdf_media_store.py."""
    msg = f"{method.upper()}\n{key}\n{int(exp)}".encode()
    return hmac.new(key_secret.encode(), msg, hashlib.sha256).hexdigest()


class ModalVolumeBackend:
    """Signs URLs for the ``services/media-store`` endpoint; no network needed to sign."""

    scheme = "modal-vol"

    def __init__(self, base_url: str, signing_key: str, bucket: str = "rdf-media"):
        self.base_url = base_url.rstrip("/")
        self._key = signing_key
        self.bucket = bucket

    def _url(self, method: str, key: str, ttl_s: int) -> tuple[str, int]:
        exp = int(time.time()) + _ttl(ttl_s)
        sig = _signature(self._key, method, key, exp)
        return f"{self.base_url}/o/{quote(key, safe='/')}?exp={exp}&sig={sig}", exp

    def _key_of(self, uri_or_key: str) -> str:
        if "://" in uri_or_key:
            scheme, bucket, key = parse_uri(uri_or_key)
            if scheme != self.scheme:
                raise StorageError(f"uri scheme {scheme!r} is not handled by the modal backend")
            return key
        return validate_key(uri_or_key)

    def signed_put(self, key: str, content_type: str, *, ttl_s: int = DEFAULT_PUT_TTL_S) -> SignedUrl:
        key = validate_key(key)
        url, exp = self._url("PUT", key, ttl_s)
        return SignedUrl(url, "PUT", make_uri(self.scheme, self.bucket, key), key, exp,
                         headers={"Content-Type": content_type}, content_type=content_type)

    def signed_get(self, uri_or_key: str, *, ttl_s: int = DEFAULT_GET_TTL_S) -> SignedUrl:
        key = self._key_of(uri_or_key)
        url, exp = self._url("GET", key, ttl_s)
        return SignedUrl(url, "GET", make_uri(self.scheme, self.bucket, key), key, exp,
                         content_type=guess_content_type(key))

    def put(self, key: str, data: bytes | Path, content_type: str | None = None) -> str:
        import httpx

        signed = self.signed_put(key, content_type or guess_content_type(key))
        content = data.read_bytes() if isinstance(data, Path) else data
        r = httpx.put(signed.url, content=content, headers=signed.headers, timeout=600)
        if r.status_code != 200:
            raise StorageError(f"put {key} failed: {r.status_code} {r.text[:200]}")
        return signed.storage_uri

    def list(self, prefix: str = "", *, limit: int = 1000) -> list[ObjectInfo]:
        import httpx

        r = httpx.get(f"{self.base_url}/list", params={"prefix": prefix, "limit": limit},
                      headers={"Authorization": f"Bearer {self._key}"}, timeout=60)
        if r.status_code != 200:
            raise StorageError(f"list {prefix!r} failed: {r.status_code} {r.text[:200]}")
        return [ObjectInfo(o["key"], make_uri(self.scheme, self.bucket, o["key"]), o["size"], o["mtime"])
                for o in r.json()["objects"]]


# ---------- s3-compatible backend (Tigris / MinIO / R2) ----------


class S3Backend:
    """boto3 presigned URLs. Same key layout; URIs are ``s3://<bucket>/<key>``.

    Not exercised tonight (no S3/Tigris credentials). Swap plan in docs/decisions.md.
    """

    scheme = "s3"

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "auto"):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self._s3 = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            region_name=region, config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def _key_of(self, uri_or_key: str) -> str:
        if "://" in uri_or_key:
            scheme, bucket, key = parse_uri(uri_or_key)
            if scheme != self.scheme:
                raise StorageError(f"uri scheme {scheme!r} is not handled by the s3 backend")
            return key
        return validate_key(uri_or_key)

    def signed_put(self, key: str, content_type: str, *, ttl_s: int = DEFAULT_PUT_TTL_S) -> SignedUrl:
        key = validate_key(key)
        ttl = _ttl(ttl_s)
        url = self._s3.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type}, ExpiresIn=ttl)
        return SignedUrl(url, "PUT", make_uri(self.scheme, self.bucket, key), key, int(time.time()) + ttl,
                         headers={"Content-Type": content_type}, content_type=content_type)

    def signed_get(self, uri_or_key: str, *, ttl_s: int = DEFAULT_GET_TTL_S) -> SignedUrl:
        key = self._key_of(uri_or_key)
        ttl = _ttl(ttl_s)
        url = self._s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl)
        return SignedUrl(url, "GET", make_uri(self.scheme, self.bucket, key), key, int(time.time()) + ttl,
                         content_type=guess_content_type(key))

    def put(self, key: str, data: bytes | Path, content_type: str | None = None) -> str:
        key = validate_key(key)
        body = data.read_bytes() if isinstance(data, Path) else data
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type or guess_content_type(key))
        return make_uri(self.scheme, self.bucket, key)

    def list(self, prefix: str = "", *, limit: int = 1000) -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                out.append(ObjectInfo(o["Key"], make_uri(self.scheme, self.bucket, o["Key"]), o["Size"],
                                      int(o["LastModified"].timestamp())))
                if len(out) >= limit:
                    return out
        return out


# ---------- factory ----------


def build_backend(settings: MediaStoreSettings) -> StorageBackend:
    if settings.rdf_media_backend == "modal":
        return ModalVolumeBackend(settings.rdf_media_store_url, settings.rdf_media_signing_key.get_secret_value(),
                                  settings.rdf_media_bucket)
    missing = [n for n in ("storage_endpoint", "storage_bucket", "storage_access_key", "storage_secret_key")
               if getattr(settings, n) in (None, "")]
    if missing:
        raise StorageError("RDF_MEDIA_BACKEND=s3 needs " + ", ".join(m.upper() for m in missing))
    return S3Backend(settings.storage_endpoint, settings.storage_bucket,  # type: ignore[arg-type]
                     settings.storage_access_key.get_secret_value(),  # type: ignore[union-attr]
                     settings.storage_secret_key.get_secret_value(),  # type: ignore[union-attr]
                     settings.storage_region)


@lru_cache(maxsize=1)
def get_backend() -> StorageBackend:
    """The configured backend (cached). Exits non-zero naming missing env vars."""
    return build_backend(load_settings(MediaStoreSettings, "orchestrator.storage"))


def backend_for_uri(uri: str) -> StorageBackend:
    """Resolve a backend by URI scheme, so old URIs still work after a swap."""
    scheme, bucket, _ = parse_uri(uri)
    current = get_backend()
    if scheme == current.scheme:
        return current
    settings = load_settings(MediaStoreSettings, "orchestrator.storage")
    if scheme == ModalVolumeBackend.scheme:
        return ModalVolumeBackend(settings.rdf_media_store_url, settings.rdf_media_signing_key.get_secret_value(), bucket)
    if scheme == S3Backend.scheme:
        alt = settings.model_copy(update={"rdf_media_backend": "s3"})
        return build_backend(alt)
    raise StorageError(f"no backend for scheme {scheme!r}")
