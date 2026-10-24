# media-store: episode media on a Modal Volume (TDM-4)

Per-camera mp4s, actions parquet and thumbnails live on the Modal Volume
`rdf-media`. This directory holds the one Modal app that fronts it with signed
upload/download URLs. Decision and swap plan: `docs/decisions.md`.

```
Jetson / browser --PUT signed url-->  https://jaxonp808--rdf-media.modal.run/o/<key>   (this app)
dashboard <video> --GET signed url-->  same, with Range support                          |
Modal training job ---- mounts modal.Volume.from_name("rdf-media") at /media -----------+--> /media/<key>
orchestrator  --signs locally with RDF_MEDIA_SIGNING_KEY-->  rdf_orchestrator.storage
```

Key layout everywhere: `tickets/{ticket_id}/episodes/{episode_id}/{filename}`.
`storage_uri` in SpacetimeDB: `modal-vol://rdf-media/<key>`.

## Deploy / rotate

```bash
cd services/media-store
uv sync
# the HMAC key: the same value lives in .env (RDF_MEDIA_SIGNING_KEY) and in a Modal Secret
modal secret create rdf-media-store RDF_MEDIA_SIGNING_KEY=<value> --force
uv run modal deploy rdf_media_store.py          # prints the URL -> RDF_MEDIA_STORE_URL in .env
```

Endpoint scales to zero (first request after idle costs a 2-4 s cold start).
For demo day set `min_containers=1` on `web` in `rdf_media_store.py` and redeploy.

## Orchestrator API (`services/orchestrator/src/rdf_orchestrator/media_api.py`)

| Route | Purpose |
|---|---|
| `POST /uploads/sign` `{ticket_id, episode_id, filename, content_type?}` | presigned PUT (`upload_url`, `headers`) plus the final `storage_uri` |
| `POST /uploads/sign-batch` `{ticket_id, episode_id, files:[{filename, content_type?}]}` | same, for a whole episode |
| `GET /media/sign?uri=modal-vol://...` | presigned GET for the video player |
| `GET /media/list?ticket_id=&episode_id=` | objects under a ticket / episode |
| `GET /media/healthz` | backend name and bucket |

Uploader contract (Jetson, browser): `PUT upload_url` with body = file bytes and
the returned `headers` (just `Content-Type`). Response `{ok, size, sha256}`.

## Reading from a Modal job

```python
media = modal.Volume.from_name("rdf-media")

@app.function(volumes={"/media": media}, gpu="H100")
def train(ticket_id: str):
    for ep in os.listdir(f"/media/tickets/{ticket_id}/episodes"): ...
```

Files committed by the endpoint are visible to containers started afterwards;
a long-running container calls `media.reload()` to see new uploads.

## Prove it

```bash
cd services/orchestrator && uv run python scripts/tdm4_prove_storage.py --mb 5 --modal-read
cd services/media-store && uv run modal run prove_modal_read.py --prefix tickets/<ticket>
```

## Local unit test (no Modal)

```bash
cd services/media-store && uv run pytest -q
```
