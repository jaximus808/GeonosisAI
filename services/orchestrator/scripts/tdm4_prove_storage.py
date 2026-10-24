#!/usr/bin/env python
"""End-to-end proof for the episode media store (TDM-4).

    cd services/orchestrator && uv run python scripts/tdm4_prove_storage.py [--mb 5] [--modal-read]

What it proves, in order:
  1. the orchestrator media API signs an upload (POST /uploads/sign)
  2. curl PUTs an N MB random file to the signed URL (what the Jetson / browser do)
  3. GET /media/sign returns a signed GET; curl downloads it; sha256 matches
  4. the signed GET honours Range (HTTP 206) -> video seeking works
  5. a real mp4 (ffmpeg testsrc) uploads and is served as video/mp4 -> browser-playable URL printed
  6. GET /media/list sees both objects
  7. (--modal-read) a Modal function mounts the Volume and reads the same bytes back (sha256 matches)

It starts the media API itself on a free port unless ORCH_URL is set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
ORCH_DIR = HERE.parents[1]
REPO = HERE.parents[3]
MEDIA_STORE_DIR = REPO / "services" / "media-store"


def http_json(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def curl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["curl", "-sS", "-m", "600", *args], capture_output=True, text=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for part in iter(lambda: f.read(1 << 20), b""):
            h.update(part)
    return h.hexdigest()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_api() -> tuple[str, subprocess.Popen]:
    port = free_port()
    proc = subprocess.Popen(
        ["uv", "run", "--quiet", "uvicorn", "rdf_orchestrator.media_api:app", "--port", str(port), "--log-level", "warning"],
        cwd=ORCH_DIR,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            http_json("GET", url + "/media/healthz")
            return url, proc
        except Exception:
            if proc.poll() is not None:
                sys.exit("media API exited early")
            time.sleep(0.2)
    proc.kill()
    sys.exit("media API did not come up")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=5)
    ap.add_argument("--modal-read", action="store_true", help="also read the object back from inside a Modal function")
    ap.add_argument("--ticket", default="tdm4-proof")
    args = ap.parse_args()

    orch = os.environ.get("ORCH_URL")
    proc = None
    if not orch:
        orch, proc = start_api()
    episode = f"ep-{int(time.time())}"
    tmp = Path(tempfile.mkdtemp(prefix="tdm4-"))
    ok = True

    def step(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    try:
        health = http_json("GET", orch + "/media/healthz")
        step("media API up", health.get("ok") is True, f"backend={health.get('media_backend')} bucket={health.get('bucket')}")

        # 1-2: sign + upload an N MB blob with curl
        blob = tmp / "cam0.bin"
        blob.write_bytes(os.urandom(args.mb * 1024 * 1024))
        blob_sha = sha256_of(blob)
        signed = http_json("POST", orch + "/uploads/sign",
                           {"ticket_id": args.ticket, "episode_id": episode, "filename": "cam0.bin",
                            "content_type": "application/octet-stream"})
        step("POST /uploads/sign", signed["method"] == "PUT" and signed["storage_uri"].endswith(f"/{args.ticket}/episodes/{episode}/cam0.bin"),
             signed["storage_uri"])
        t0 = time.time()
        r = curl("-X", "PUT", "-T", str(blob), "-H", "Content-Type: application/octet-stream", "-w", "\n%{http_code}", signed["upload_url"])
        body, _, code = r.stdout.rpartition("\n")
        put_json = json.loads(body) if code == "200" else {}
        step(f"curl PUT {args.mb} MB", code == "200" and put_json.get("sha256") == blob_sha,
             f"http {code}, {time.time() - t0:.1f}s, server sha256 matches={put_json.get('sha256') == blob_sha}")

        # 3: signed GET + download + compare
        get = http_json("GET", orch + "/media/sign?" + urllib.parse.urlencode({"uri": signed["storage_uri"]}))
        out = tmp / "cam0.down"
        r = curl("-o", str(out), "-w", "%{http_code}", get["url"])
        step("curl GET via /media/sign", r.stdout == "200" and sha256_of(out) == blob_sha, f"http {r.stdout}, sha256 matches")

        # 4: range request
        r = curl("-r", "100-199", "-o", "/dev/null", "-D", "-", get["url"])
        head = r.stdout
        step("Range request -> 206", " 206 " in head.splitlines()[0] and "content-range: bytes 100-199/" in head.lower(),
             head.splitlines()[0].strip())

        # 5: a real mp4
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            mp4 = tmp / "cam0.mp4"
            subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15", "-t", "2",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)], check=True)
            s2 = http_json("POST", orch + "/uploads/sign",
                           {"ticket_id": args.ticket, "episode_id": episode, "filename": "cam0.mp4", "content_type": "video/mp4"})
            r = curl("-X", "PUT", "-T", str(mp4), "-H", "Content-Type: video/mp4", "-o", "/dev/null", "-w", "%{http_code}", s2["upload_url"])
            g2 = http_json("GET", orch + "/media/sign?" + urllib.parse.urlencode({"uri": s2["storage_uri"]}))
            h = curl("-I", g2["url"]).stdout.lower()
            step("mp4 upload + browser-playable signed GET", r.stdout == "200" and "content-type: video/mp4" in h and "accept-ranges: bytes" in h,
                 "video/mp4 + Accept-Ranges")
            print(f"       play it: {g2['url']}")
        else:
            step("mp4 upload", False, "ffmpeg not installed; skipped")

        # 6: list
        lst = http_json("GET", orch + "/media/list?" + urllib.parse.urlencode({"ticket_id": args.ticket, "episode_id": episode}))
        keys = sorted(o["key"].rsplit("/", 1)[-1] for o in lst["objects"])
        step("GET /media/list", "cam0.bin" in keys, ", ".join(keys))

        # 7: Modal-side read through the Volume mount
        if args.modal_read:
            key = signed["key"]
            r = subprocess.run(["uv", "run", "--quiet", "modal", "run", "prove_modal_read.py", "--key", key],
                               cwd=MEDIA_STORE_DIR, capture_output=True, text=True)
            line = next((ln for ln in r.stdout.splitlines() if ln.startswith("{")), "{}")
            res = json.loads(line) if line else {}
            step("Modal function reads the object off the Volume", res.get("found") is True and res.get("sha256") == blob_sha,
                 f"size={res.get('size')} sha256 matches={res.get('sha256') == blob_sha}" if res else (r.stderr[-300:] or r.stdout[-300:]))
    finally:
        if proc:
            proc.terminate()
        shutil.rmtree(tmp, ignore_errors=True)

    print("ALL PASS" if ok else "SOME STEPS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
