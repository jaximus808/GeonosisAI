#!/usr/bin/env python3
"""TDM-41: warm up / warm down / inspect / hot-swap the policy server (rdf-training `Policy`).

    make policy-up                     # min_containers=1, then wait for GET /health (cold start ~1 min)
    make policy-down                   # min_containers=0 AND stop the running container now
    make policy-status                 # URL, autoscaler intent, running containers (does NOT wake it)
    cd services/training && uv run --with modal --with httpx python scripts/policy_ctl.py health
    ... policy_ctl.py activate modal-vol://rdf-checkpoints/runs/ticket-42/<run>/1999 [--preload]
    ... policy_ctl.py activate base

`up` bills one GPU (H100) for as long as it stays up: run it a few minutes before a robot
session and `down` right after. The overnight run (TDM-72) deliberately keeps it `up`.
The bearer token comes from POLICY_SERVER_TOKEN (env, else the repo-root .env); it is never printed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import modal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sync_modal_secret import REPO_ROOT, read_dotenv  # noqa: E402 - sibling script (TDM-38)

APP, CLS = "rdf-training", "Policy"


def _policy():
    return modal.Cls.from_name(APP, CLS)()


def url() -> str:
    return (os.environ.get("POLICY_SERVER_URL") or _policy().web.get_web_url()).rstrip("/")


def token() -> str:
    t = os.environ.get("POLICY_SERVER_TOKEN") or read_dotenv(REPO_ROOT / ".env").get("POLICY_SERVER_TOKEN", "")
    if not t:
        sys.exit("POLICY_SERVER_TOKEN is not set (env or .env)")
    return t


def containers() -> list[dict]:
    app_id = None
    try:
        app_id = modal.App.lookup(APP).app_id
    except Exception:  # noqa: BLE001
        return []
    r = subprocess.run(["modal", "container", "list", "--app-id", app_id, "--json"], capture_output=True, text=True)
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []


def wait_health(timeout_s: float = 1200) -> dict:
    import httpx

    t0 = time.monotonic()
    u = f"{url()}/health"
    while time.monotonic() - t0 < timeout_s:
        try:
            r = httpx.get(u, timeout=60)
            if r.status_code == 200 and r.json().get("ready"):
                h = r.json()
                h["_waited_s"] = round(time.monotonic() - t0, 1)
                return h
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise SystemExit(f"policy server not healthy after {timeout_s:.0f} s")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd == "up":
        _policy().update_autoscaler(min_containers=1)
        print("min_containers=1: one GPU container kept warm (billed until `policy-down`)")
        if "--no-wait" not in argv:
            h = wait_health()
            print(json.dumps({k: h.get(k) for k in ("active_checkpoint_uri", "gpu", "startup_s", "_waited_s")}))
    elif cmd == "down":
        _policy().update_autoscaler(min_containers=0)
        print("min_containers=0: scale to zero")
        for c in containers():
            cid = c.get("Container ID") or c.get("container_id") or c.get("id")
            if cid:
                subprocess.run(["modal", "container", "stop", cid], capture_output=True, text=True)
                print(f"stopped container {cid}")
    elif cmd == "status":
        print("web url:", url())
        cs = containers()
        print(f"running containers: {len(cs)}")
        for c in cs:
            print("  ", json.dumps(c))
    elif cmd == "health":
        import httpx

        print(json.dumps(httpx.get(f"{url()}/health", timeout=600).json(), indent=2))
    elif cmd == "activate":
        import httpx

        if len(argv) < 2:
            sys.exit("usage: policy_ctl.py activate <checkpoint_uri|base> [--preload]")
        r = httpx.post(
            f"{url()}/policy/activate",
            json={"checkpoint_uri": argv[1], "preload_only": "--preload" in argv},
            headers={"Authorization": f"Bearer {token()}"},
            timeout=900,
        )
        print(r.status_code, json.dumps(r.json(), indent=2))
        return 0 if r.status_code < 300 else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
