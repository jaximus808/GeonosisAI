"""TDM-78: set up the two secrets the robot page's command routes use. Values are never printed.

    cd services/orchestrator
    uv run python ../../spikes/tdm-78-robot-page/setup_web_auth.py operator-key
        Writes RDF_OPERATOR_KEY (random) to the repo .env if it isn't set. The orchestrator's
        POST /robots/{id}/commands and the web server's /api/tdm78 proxy both read it from there.
        Without it every motion command from the page answers 503 (the soft e-stop still works).

    uv run python ../../spikes/tdm-78-robot-page/setup_web_auth.py mint-web-identity --yes
        WRITES TO THE DATABASE in $SPACETIME_HOST/$SPACETIME_DB_NAME: mints a fresh identity
        (POST /v1/identity), allow-lists it as role `web` with the owner token
        (register_service_identity), and stores its token in .env as RDF_WEB_SPACETIME_TOKEN.
        That gives the soft e-stop its direct path (web server -> SpacetimeDB, no orchestrator).
        Without it the e-stop falls back to the orchestrator route.

Restart the web server and the orchestrator afterwards so they pick the values up.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENV = Path(os.environ.get("RDF_ENV_FILE", REPO / ".env"))


def _env_has(key: str) -> bool:
    if os.environ.get(key, "").strip():
        return True
    if not ENV.exists():
        return False
    for line in ENV.read_text().splitlines():
        k, _, v = line.partition("=")
        if k.strip().removeprefix("export ").strip() == key and v.strip().strip("'\""):
            return True
    return False


def _append(key: str, value: str) -> None:
    text = ENV.read_text() if ENV.exists() else ""
    sep = "" if not text or text.endswith("\n") else "\n"
    ENV.write_text(f"{text}{sep}# TDM-78 robot page\n{key}={value}\n")
    os.chmod(ENV, 0o600)
    print(f"{key}: written to {ENV}")


def _dotenv(key: str) -> str | None:
    v = os.environ.get(key, "").strip()
    if v:
        return v
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            k, _, val = line.partition("=")
            if k.strip().removeprefix("export ").strip() == key:
                return val.strip().strip("'\"") or None
    return None


def operator_key() -> None:
    if _env_has("RDF_OPERATOR_KEY"):
        print("RDF_OPERATOR_KEY: already set (unchanged)")
        return
    _append("RDF_OPERATOR_KEY", secrets.token_urlsafe(32))


def mint_web_identity(yes: bool) -> None:
    if _env_has("RDF_WEB_SPACETIME_TOKEN"):
        print("RDF_WEB_SPACETIME_TOKEN: already set (unchanged)")
        return
    host = (_dotenv("SPACETIME_HOST") or "").rstrip("/")
    db = _dotenv("SPACETIME_DB_NAME")
    owner = _dotenv("SPACETIME_TOKEN")
    if not (host and db and owner):
        sys.exit("SPACETIME_HOST, SPACETIME_DB_NAME and SPACETIME_TOKEN (owner) must be set")
    if not yes:
        sys.exit(f"this registers a new `web` service identity in {db} on {host}; rerun with --yes")
    with urllib.request.urlopen(urllib.request.Request(f"{host}/v1/identity", data=b"", method="POST"), timeout=20) as r:
        d = json.loads(r.read())
    body = json.dumps([{"__identity__": "0x" + d["identity"]}, "web", "web@laptop (TDM-78 robot page e-stop)"]).encode()
    req = urllib.request.Request(f"{host}/v1/database/{db}/call/register_service_identity", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {owner}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status >= 300:
            sys.exit(f"register_service_identity failed: HTTP {r.status}")
    print(f"registered identity 0x{d['identity'][:12]}... as role web in {db}")
    _append("RDF_WEB_SPACETIME_TOKEN", d["token"])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["operator-key", "mint-web-identity"])
    p.add_argument("--yes", action="store_true")
    a = p.parse_args()
    operator_key() if a.cmd == "operator-key" else mint_web_identity(a.yes)


if __name__ == "__main__":
    main()
