"""Prove a Modal job can read episode media straight off the Volume (TDM-4).

This is exactly how a training job gets at the data: mount ``rdf-media`` and
read files under ``/media``. No HTTP, no copy.

    cd services/media-store
    uv run modal run prove_modal_read.py --key tickets/<t>/episodes/<e>/cam0.mp4
    uv run modal run prove_modal_read.py --prefix tickets/<t>/episodes/<e>
"""

from __future__ import annotations

import modal

# Same values as rdf_media_store.py (not imported: `modal run` ships only this file).
MEDIA_ROOT = "/media"
VOLUME_NAME = "rdf-media"

app = modal.App("rdf-media-store-prove-read")
media_volume = modal.Volume.from_name(VOLUME_NAME)


@app.function(volumes={MEDIA_ROOT: media_volume}, timeout=300)
def read_object(key: str) -> dict:
    import hashlib
    import os

    path = os.path.join(MEDIA_ROOT, key)
    if not os.path.isfile(path):
        return {"key": key, "found": False}
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        for part in iter(lambda: f.read(1 << 20), b""):
            h.update(part)
            size += len(part)
    return {"key": key, "found": True, "size": size, "sha256": h.hexdigest()}


@app.function(volumes={MEDIA_ROOT: media_volume}, timeout=300)
def list_prefix(prefix: str) -> list[dict]:
    import os

    base = os.path.join(MEDIA_ROOT, prefix.strip("/"))
    out = []
    for dirpath, _d, files in os.walk(base):
        for name in files:
            p = os.path.join(dirpath, name)
            out.append({"key": os.path.relpath(p, MEDIA_ROOT), "size": os.path.getsize(p)})
    return sorted(out, key=lambda e: e["key"])


@app.local_entrypoint()
def main(key: str = "", prefix: str = ""):
    import json

    if key:
        print(json.dumps(read_object.remote(key)))
    if prefix:
        print(json.dumps(list_prefix.remote(prefix), indent=1))
    if not key and not prefix:
        print("pass --key <object key> and/or --prefix <dir prefix>")
