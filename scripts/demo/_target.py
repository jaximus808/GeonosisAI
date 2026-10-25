"""Shared target guard for scripts/demo/cancel_old_tickets.py and seed_samples.py.

``--target local``     a throwaway local SpacetimeDB (the TDM-28 harness's 127.0.0.1:3028/tdm28loop by
                       default, or --host/--db). Non-local hosts are refused.
``--target maincloud`` https://maincloud.spacetimedb.com / robo-factory-a706j (SPACETIME_HOST/DB_NAME
                       from .env). --host/--db are refused here so a typo can't point it somewhere else.

The token is always SPACETIME_TOKEN from .env (the owner login; on a module you published locally with
the same CLI login it is the owner there too). It is never printed.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from rdf_common.spacetime_client import SpacetimeClient, load_dotenv

LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
MAINCLOUD_HOST = "https://maincloud.spacetimedb.com"
MAINCLOUD_DB = "robo-factory-a706j"
LOCAL_DEFAULT_HOST = "http://127.0.0.1:3028"
LOCAL_DEFAULT_DB = "tdm28loop"
# The user's live stack: never aim a local run at these.
LIVE_PORTS = {8028, 3737, 8791, 8792, 8765, 3767}


def die(tag: str, msg: str) -> None:
    print(f"[{tag}] REFUSED: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def add_target_args(ap) -> None:
    ap.add_argument("--target", required=True, choices=("local", "maincloud"))
    ap.add_argument("--host", help=f"local only (default {LOCAL_DEFAULT_HOST})")
    ap.add_argument("--db", help=f"local only (default {LOCAL_DEFAULT_DB})")
    ap.add_argument("--yes", action="store_true", help="actually write (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing (the default)")


def resolve(args, tag: str) -> tuple[str, str, str]:
    """-> (host, db, token). Refuses a mismatched target."""
    load_dotenv()
    token = os.environ.get("SPACETIME_TOKEN", "")
    if not token:
        die(tag, "SPACETIME_TOKEN is not set in .env")
    if args.target == "maincloud":
        if args.host or args.db:
            die(tag, "--host/--db are for --target local only")
        host = (os.environ.get("SPACETIME_HOST") or MAINCLOUD_HOST).rstrip("/")
        db = os.environ.get("SPACETIME_DB_NAME") or MAINCLOUD_DB
        if "maincloud" not in host or db != MAINCLOUD_DB:
            die(tag, f"SPACETIME_HOST/DB_NAME in .env are not maincloud {MAINCLOUD_DB} ({host}, {db})")
        return host, db, token
    host = (args.host or LOCAL_DEFAULT_HOST).rstrip("/")
    db = args.db or LOCAL_DEFAULT_DB
    u = urlparse(host)
    if u.scheme != "http" or (u.hostname or "").lower() not in LOCAL_HOSTNAMES:
        die(tag, f"--target local needs a local http host, got {host!r}")
    if u.port in LIVE_PORTS:
        die(tag, f"port {u.port} belongs to the live stack")
    if db == MAINCLOUD_DB and u.port is None:
        die(tag, "that looks like the maincloud database name on a local host; pass --db")
    return host, db, token


def client(host: str, db: str, token: str) -> SpacetimeClient:
    return SpacetimeClient(host, db, token)


def table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "  (none)"
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    w = {c: min(v, 60) for c, v in w.items()}
    line = "  " + "  ".join(c.ljust(w[c]) for c in cols)
    out = [line, "  " + "  ".join("-" * w[c] for c in cols)]
    for r in rows:
        out.append("  " + "  ".join(str(r.get(c, ""))[: w[c]].ljust(w[c]) for c in cols))
    return "\n".join(out)
