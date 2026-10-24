"""TDM-44: give the Jetson its own `robot_client` SpacetimeDB identity instead of the owner token.

    scripts/jetson/jetson.sh mint-identity          # plan only: says what it would do
    scripts/jetson/jetson.sh mint-identity --yes    # do it (writes to the database + .env)
    make jetson-env                                 # then push it to the Jetson

Steps (docs/pre-run-checklist.md "Separate service identities"):
  1. POST /v1/identity -> a fresh identity + token
  2. as the owner (SPACETIME_TOKEN in .env): register_service_identity(identity, "robot_client", label)
  3. append ROBOT_CLIENT_SPACETIME_TOKEN=<token> to .env (jetson.sh env prefers it over SPACETIME_TOKEN)
Idempotent: if ROBOT_CLIENT_SPACETIME_TOKEN is already in .env it does nothing. Never prints tokens;
the identity hex (public) is printed so you can find it in event_log.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
from pathlib import Path

import httpx

KEY = "ROBOT_CLIENT_SPACETIME_TOKEN"


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="mint + allow-list a robot_client identity for the Jetson")
    ap.add_argument("--env-file", default=str(Path(__file__).resolve().parents[2] / ".env"))
    ap.add_argument("--label", default=None, help="service_identity.label (default robot_client@jetson)")
    ap.add_argument("--yes", action="store_true", help="actually create and register it")
    a = ap.parse_args()
    envp = Path(a.env_file)
    if not envp.is_file():
        print(f"{envp} not found", file=sys.stderr)
        return 2
    env = {**read_env(envp), **{k: v for k, v in os.environ.items() if k.startswith("SPACETIME_") and v}}
    if env.get(KEY):
        print(f"{KEY} is already set in {envp}; nothing to do. Push it with: make jetson-env")
        return 0
    host, db, owner = env.get("SPACETIME_HOST", "").rstrip("/"), env.get("SPACETIME_DB_NAME"), env.get("SPACETIME_TOKEN")
    if not (host and db and owner):
        print("SPACETIME_HOST / SPACETIME_DB_NAME / SPACETIME_TOKEN (owner) must be set in .env", file=sys.stderr)
        return 2
    label = a.label or "robot_client@jetson"
    print(f"target: {host} db {db}")
    if not a.yes:
        print(f"plan: POST {host}/v1/identity; register_service_identity(<new>, 'robot_client', '{label}') as owner;"
              f" append {KEY} to {envp}. Re-run with --yes to do it.")
        return 0
    with httpx.Client(timeout=20) as c:
        r = c.post(f"{host}/v1/identity")
        r.raise_for_status()
        d = r.json()
        ident, token = d["identity"], d["token"]
        ident_hex = ident if ident.startswith("0x") else "0x" + ident
        r = c.post(f"{host}/v1/database/{db}/call/register_service_identity",
                   headers={"Authorization": f"Bearer {owner}", "Content-Type": "application/json"},
                   content=json.dumps([[ident_hex], "robot_client", label]))
        if r.status_code >= 300:
            print(f"register_service_identity failed: HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
            print("nothing written to .env (the unregistered identity is harmless)", file=sys.stderr)
            return 1
    # sanity: the token's claims carry the identity we registered (decode locally, print nothing secret)
    try:
        p = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        if claims.get("hex_identity") and claims["hex_identity"].lower() != ident_hex[2:].lower():
            print("WARN: token claim hex_identity differs from the registered identity", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"note: could not decode token claims ({type(e).__name__}); skipped the identity cross-check", file=sys.stderr)
    text = envp.read_text()
    with envp.open("a") as f:
        if text and not text.endswith("\n"):
            f.write("\n")
        f.write(f"# TDM-44: robot_client service identity {ident_hex} (minted {socket.gethostname()})\n{KEY}={token}\n")
    print(f"registered robot_client identity {ident_hex} (label {label}); token stored in {envp} as {KEY}")
    print("next: make jetson-env   (then the Jetson uses it as SPACETIME_TOKEN)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
