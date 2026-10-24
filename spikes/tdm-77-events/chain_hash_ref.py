#!/usr/bin/env python3
"""Reference implementation of the event_log chain hash "rdf-evlog-chain-v1" (TDM-77).

The /events page computes the same hash in the browser (apps/web/src/lib/eventlog/chain.ts).
TDM-15's scripts/verify_event_log.py should reuse `chain_summary` so the two always agree.

    rows   = every event_log row, ordered by id ascending
    rec    = UTF-8 of f"{id}\\n{ts_micros}\\n{len(actor)}:{actor}\\n{len(type)}:{type}\\n{len(payload)}:{payload}"
             (len = UTF-8 byte length of the string after the colon)
    h_0    = 32 zero bytes;  h_i = sha256(h_{i-1} || rec_i);  print hex(h_n)

Usage (standard library only; reads SPACETIME_HOST / SPACETIME_DB_NAME / SPACETIME_TOKEN from
the environment or ../../.env, and never prints the token):

    python3 -I spikes/tdm-77-events/chain_hash_ref.py            # maincloud
    python3 -I spikes/tdm-77-events/chain_hash_ref.py --selftest
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ALGORITHM = "rdf-evlog-chain-v1"


def _field(s: str) -> str:
    return f"{len(s.encode('utf-8'))}:{s}"


def canonical_record(row_id: int, ts_micros: int, actor: str, type_: str, payload_json: str) -> bytes:
    return f"{row_id}\n{ts_micros}\n{_field(actor)}\n{_field(type_)}\n{_field(payload_json)}".encode("utf-8")


def chain_summary(rows: list[tuple[int, int, str, str, str]]) -> dict:
    """rows: (id, ts_micros, actor, type, payload_json), any order."""
    rows = sorted(rows, key=lambda r: r[0])
    h = bytes(32)
    for r in rows:
        h = hashlib.sha256(h + canonical_record(*r)).digest()
    ids = [r[0] for r in rows]
    gaps = [(a, b) for a, b in zip(ids, ids[1:]) if b - a > 1]
    return {
        "algorithm": ALGORITHM,
        "count": len(rows),
        "first_id": ids[0] if ids else None,
        "last_id": ids[-1] if ids else None,
        "first_ts_micros": rows[0][1] if rows else None,
        "last_ts_micros": rows[-1][1] if rows else None,
        "gaps": gaps,
        "hash": h.hex(),
    }


def _load_env() -> None:
    env = Path(__file__).resolve().parents[2] / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch_rows() -> list[tuple[int, int, str, str, str]]:
    _load_env()
    host = os.environ.get("SPACETIME_HOST", "https://maincloud.spacetimedb.com").rstrip("/")
    db = os.environ.get("SPACETIME_DB_NAME", "robo-factory-a706j")
    req = urllib.request.Request(
        f"{host}/v1/database/{db}/sql",
        data=b"SELECT id, ts, actor, type, payload_json FROM event_log",
        method="POST",
    )
    token = os.environ.get("SPACETIME_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and Path("/etc/ssl/cert.pem").exists():  # python.org macOS build
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        stmt = json.load(resp)[0]
    out = []
    for r in stmt["rows"]:
        ts = r[1][0] if isinstance(r[1], list) else r[1]["__timestamp_micros_since_unix_epoch__"]
        out.append((int(r[0]), int(ts), r[2], r[3], r[4]))
    return out


def _iso(micros: int | None) -> str:
    if micros is None:
        return "-"
    return datetime.fromtimestamp(micros / 1e6, tz=timezone.utc).isoformat(timespec="seconds")


def _selftest() -> None:
    # Fixed vectors shared with apps/web (spikes/tdm-77-events/parity_check.mts).
    assert chain_summary([])["hash"] == "0" * 64
    rows = [
        (1, 1792824327305282, "setup-agent", "infra.smoke_test", '{"note":"héllo"}'),
        (2, 1792824327576637, "planner", "data_plan.recorded", '{"ticket_id":1,"data_type":"recovery_takeover"}'),
    ]
    print(json.dumps(chain_summary(rows)))


def main() -> None:
    if "--selftest" in sys.argv:
        _selftest()
        return
    rows = fetch_rows()
    if "--dump" in sys.argv:  # raw rows for parity_check.mts
        print(json.dumps([list(r) for r in rows]))
        return
    s = chain_summary(rows)
    if "--json" in sys.argv:
        print(json.dumps(s))
        return
    print(f"algorithm  {s['algorithm']}")
    print(f"rows       {s['count']}  (ids {s['first_id']}..{s['last_id']}, gaps: {s['gaps'] or 'none'})")
    print(f"first ts   {_iso(s['first_ts_micros'])}")
    print(f"last ts    {_iso(s['last_ts_micros'])}")
    print(f"chain hash {s['hash']}")


if __name__ == "__main__":
    main()
