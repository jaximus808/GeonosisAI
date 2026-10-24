"""rdf-media-store: episode media on a Modal Volume behind a signed-URL endpoint.

Why this exists (TDM-4, see docs/decisions.md): the Jetson, the browser and
Modal training jobs all need the same blobs tonight, and the only credentials we
have are Modal's. So media lives on the Modal Volume ``rdf-media`` and this tiny
FastAPI app, deployed on Modal, is the S3-presigned-URL stand-in:

    PUT  /o/{key}?exp=&sig=     upload one object (streamed, up to 4 GiB)
    GET  /o/{key}?exp=&sig=     download; supports Range (video seeking)
    HEAD /o/{key}?exp=&sig=     headers only
    GET  /list?prefix=          list objects (Authorization: Bearer <signing key>)
    GET  /healthz

Training jobs do not go through HTTP at all: they mount the same Volume
(``modal.Volume.from_name("rdf-media")`` at ``/media``) and read
``/media/tickets/<ticket>/episodes/<episode>/<file>`` directly.

Signing: ``sig = hex(HMAC_SHA256(RDF_MEDIA_SIGNING_KEY, f"{METHOD}\\n{key}\\n{exp}"))``
where ``exp`` is a unix timestamp. The orchestrator signs with the same key
(``services/orchestrator/src/rdf_orchestrator/storage.py``); the two
``_signature`` functions must stay identical. HEAD accepts a GET signature so
one signed URL serves both.

Deploy:   cd services/media-store && uv run modal deploy rdf_media_store.py
Secret:   modal secret create rdf-media-store RDF_MEDIA_SIGNING_KEY=<same value as .env>
"""

# NOTE: no `from __future__ import annotations` here: FastAPI must see real
# `Request` types for handlers defined inside web(), or it treats them as query params.
import modal

APP_NAME = "rdf-media-store"
VOLUME_NAME = "rdf-media"
SECRET_NAME = "rdf-media-store"
WEB_LABEL = "rdf-media"  # -> https://<workspace>--rdf-media.modal.run
MEDIA_ROOT = "/media"

app = modal.App(APP_NAME)
media_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
image = modal.Image.debian_slim(python_version="3.11").pip_install("fastapi[standard]==0.115.12")


def _signature(key_secret: str, method: str, key: str, exp: int) -> str:
    """Keep byte-for-byte identical to rdf_orchestrator.storage._signature."""
    import hashlib
    import hmac

    msg = f"{method.upper()}\n{key}\n{int(exp)}".encode()
    return hmac.new(key_secret.encode(), msg, hashlib.sha256).hexdigest()


@app.function(
    image=image,
    volumes={MEDIA_ROOT: media_volume},
    secrets=[modal.Secret.from_name(SECRET_NAME, required_keys=["RDF_MEDIA_SIGNING_KEY"])],
    timeout=600,
    scaledown_window=300,
    max_containers=4,
    cpu=1.0,
    memory=1024,
)
@modal.concurrent(max_inputs=64)
@modal.asgi_app(label=WEB_LABEL)
def web():
    import os

    return build_api(os.environ["RDF_MEDIA_SIGNING_KEY"], MEDIA_ROOT, media_volume)


def build_api(signing_key: str, media_root: str, volume):
    """The FastAPI app. Module-level so it can be unit-tested without Modal."""
    import hmac
    import mimetypes
    import os
    import re
    import time
    from hashlib import sha256
    from pathlib import Path

    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    root = Path(media_root)
    chunk = 1024 * 1024
    key_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(/[A-Za-z0-9][A-Za-z0-9._\-]*)*$")
    mimetypes.add_type("application/vnd.apache.parquet", ".parquet")
    mimetypes.add_type("video/mp4", ".mp4")

    api = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "HEAD", "PUT", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Length", "Content-Range", "Accept-Ranges", "ETag", "X-Sha256"],
        max_age=3600,
    )

    # ---------- helpers ----------

    def check_key(key: str) -> Path:
        if not key_re.match(key) or ".." in key.split("/") or len(key) > 1024:
            raise HTTPException(400, "bad key")
        return root / key

    def check_sig(method: str, key: str, exp: int, sig: str) -> None:
        if exp < time.time():
            raise HTTPException(403, "url expired")
        want = _signature(signing_key, method, key, exp)
        if not hmac.compare_digest(want, sig or ""):
            raise HTTPException(403, "bad signature")

    def check_bearer(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(token, signing_key):
            raise HTTPException(401, "bearer token required")

    def guess_type(key: str) -> str:
        return mimetypes.guess_type(key)[0] or "application/octet-stream"

    def try_reload() -> None:
        # Another container may have committed since we started. reload() fails
        # with "volume busy" while this container has files open; that is fine.
        try:
            volume.reload()
        except Exception:
            pass

    def stat_or_404(key: str) -> tuple[Path, int]:
        path = check_key(key)
        if not path.is_file():
            try_reload()
        if not path.is_file():
            raise HTTPException(404, "no such object")
        return path, path.stat().st_size

    def object_headers(key: str, size: int, mtime: float) -> dict[str, str]:
        return {
            "Content-Type": guess_type(key),
            "Accept-Ranges": "bytes",
            "ETag": f'"{int(mtime)}-{size}"',
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="{key.rsplit("/", 1)[-1]}"',
        }

    # ---------- routes ----------

    @api.get("/healthz")
    def healthz():
        return {"ok": True, "app": APP_NAME, "volume": VOLUME_NAME}

    @api.put("/o/{key:path}")
    async def put_object(key: str, request: Request, exp: int = Query(...), sig: str = Query(...)):
        check_sig("PUT", key, exp, sig)
        path = check_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.part-{os.getpid()}-{int(time.time() * 1000)}")
        digest = sha256()
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for part in request.stream():
                    if part:
                        f.write(part)
                        digest.update(part)
                        size += len(part)
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        volume.commit()
        return JSONResponse(
            {"ok": True, "key": key, "size": size, "sha256": digest.hexdigest()},
            headers={"ETag": f'"{digest.hexdigest()}"', "X-Sha256": digest.hexdigest()},
        )

    @api.head("/o/{key:path}")
    def head_object(key: str, exp: int = Query(...), sig: str = Query(...)):
        check_sig("GET", key, exp, sig)
        path, size = stat_or_404(key)
        headers = object_headers(key, size, path.stat().st_mtime)
        headers["Content-Length"] = str(size)
        return Response(status_code=200, headers=headers)

    @api.get("/o/{key:path}")
    def get_object(key: str, request: Request, exp: int = Query(...), sig: str = Query(...)):
        check_sig("GET", key, exp, sig)
        path, size = stat_or_404(key)
        headers = object_headers(key, size, path.stat().st_mtime)

        start, end = 0, size - 1
        status = 200
        range_header = request.headers.get("range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a == "":  # suffix range: last N bytes
                    n = int(b)
                    start, end = max(size - n, 0), size - 1
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
            except ValueError:
                raise HTTPException(400, "bad range")
            if start >= size or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1)
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = end - start + 1
        headers["Content-Length"] = str(length)

        def body():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    data = f.read(min(chunk, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(body(), status_code=status, headers=headers, media_type=headers["Content-Type"])

    @api.get("/list")
    def list_objects(request: Request, prefix: str = Query("", max_length=1024), limit: int = Query(1000, ge=1, le=10000)):
        check_bearer(request)
        prefix = prefix.strip("/")
        if prefix and (".." in prefix.split("/") or not key_re.match(prefix)):
            raise HTTPException(400, "bad prefix")
        try_reload()
        base = root / prefix if prefix else root
        entries = []
        if base.is_dir():
            for dirpath, _dirs, files in os.walk(base):
                for name in files:
                    if name.startswith("."):
                        continue
                    p = Path(dirpath) / name
                    st = p.stat()
                    entries.append({"key": str(p.relative_to(root)), "size": st.st_size, "mtime": int(st.st_mtime)})
        elif base.is_file():
            st = base.stat()
            entries.append({"key": prefix, "size": st.st_size, "mtime": int(st.st_mtime)})
        entries.sort(key=lambda e: e["key"])
        truncated = len(entries) > limit
        return {"prefix": prefix, "objects": entries[:limit], "truncated": truncated}

    return api
