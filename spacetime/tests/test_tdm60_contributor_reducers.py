#!/usr/bin/env python3
"""TDM-60 reducer tests: register_contributor, contributor_heartbeat,
contributor_offline, and the scheduled sweep_contributor_presence.

Runs against a THROWAWAY local server (it creates rows), stdlib only:

    spacetime start -l 127.0.0.1:3960 --data-dir $TMPDIR/tdm60-stdb/data --non-interactive
    cd spacetime && spacetime publish -s http://127.0.0.1:3960 tdm60test --yes
    set -a; source ../.env; set +a      # SPACETIME_TOKEN = publisher = owner (never printed)
    python3 tests/test_tdm60_contributor_reducers.py [--host ...] [--db tdm60test] [--skip-sweep]

Checks: a browser identity signs up and gets exactly one row bound to it;
re-registering updates the same row (refresh keeps identity); guest names;
validation; service identities (owner) refused; email lands only in the private
contributor_contact table; heartbeats log only transitions; the scheduled sweep
marks a silent contributor offline (waits ~110 s; --skip-sweep to skip).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

P = argparse.ArgumentParser()
P.add_argument("--host", default="http://127.0.0.1:3960")
P.add_argument("--db", default="tdm60test")
P.add_argument("--skip-sweep", action="store_true")
P.add_argument("--allow-maincloud", action="store_true", help="refused by default: this script writes rows")
A = P.parse_args()
if "maincloud" in A.host and not A.allow_maincloud:
    sys.exit("refusing to write test rows to maincloud; use a local server")
OWNER = os.environ.get("SPACETIME_TOKEN") or sys.exit("SPACETIME_TOKEN not set (source .env)")
BASE = f"{A.host}/v1/database/{A.db}"


def _req(url: str, body: bytes | None, token: str | None, ctype: str | None = None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    if ctype:
        h["Content-Type"] = ctype
    r = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def call(reducer: str, args: list, token: str | None) -> tuple[bool, str]:
    code, text = _req(f"{BASE}/call/{reducer}", json.dumps(args).encode(), token, "application/json")
    return 200 <= code < 300, text


def sql(q: str) -> list[dict]:
    code, text = _req(f"{BASE}/sql", q.encode(), OWNER)
    assert code == 200, f"sql failed {code}: {text}"
    res = json.loads(text)[0]
    cols = [e["name"]["some"] for e in res["schema"]["elements"]]
    return [dict(zip(cols, row)) for row in res["rows"]]


def anon_sql(q: str, token: str) -> tuple[int, str]:
    return _req(f"{BASE}/sql", q.encode(), token)


def new_identity() -> tuple[str, str]:
    code, text = _req(f"{A.host}/v1/identity", b"", None)
    assert code == 200, text
    d = json.loads(text)
    return d["identity"], d["token"]


def events() -> list[dict]:
    return sorted(sql("SELECT * FROM event_log"), key=lambda r: r["id"])


def ident_hex(v) -> str:
    # SQL JSON: option<identity> is [0, ["0x..."]] (some) or [1, []] (none)
    if isinstance(v, list) and len(v) == 2 and v[0] == 0:
        v = v[1]
    elif isinstance(v, list) and len(v) == 2 and v[0] == 1:
        return ""
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v).lower().removeprefix("0x")


PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if not cond:
        sys.exit(f"FAIL {name} {detail}")
    PASSED += 1
    print(f"ok   {name}")


def expect(name: str, reducer: str, args: list, token: str | None, *, ok: bool = True,
           err: str = "", new_events: list[str] | None = None) -> list[dict]:
    before = len(events())
    success, text = call(reducer, args, token)
    check(f"{name}: {'accepted' if ok else 'rejected'}", success == ok and (ok or err in text), text[:300])
    added = events()[before:]
    if new_events is not None:
        got = [e["type"] for e in added]
        check(f"{name}: events {new_events}", got == new_events, f"got {got}")
    return added


def mine(identity: str) -> list[dict]:
    h = identity.lower().removeprefix("0x")
    return [c for c in sql("SELECT * FROM contributor") if ident_hex(c["identity"]) == h]


# ---------------------------------------------------------------- sign-up
alice_id, alice = new_identity()
bob_id, bob = new_identity()

expect("owner refused", "register_contributor", ["Owner", "", [], "contributor", 0], OWNER,
       ok=False, err="service identity")
expect("bad kind", "register_contributor", ["A", "", [], "robot", 0], alice, ok=False, err="kind")
expect("bad device", "register_contributor", ["A", "", ["Phone!"], "contributor", 0], alice, ok=False, err="device")
expect("bad email", "register_contributor", ["A", "nope", [], "contributor", 0], alice, ok=False, err="email")
expect("long name", "register_contributor", ["x" * 65, "", [], "contributor", 0], alice, ok=False, err="name")
expect("heartbeat before sign-up", "contributor_heartbeat", ["marketplace"], alice, ok=False, err="not a contributor")
check("no rows from rejected calls", mine(alice_id) == [])

ev = expect("alice signs up", "register_contributor",
            ["  Alice   Judge ", "Alice@Example.com", ["phone", "phone", "android_webxr"], "contributor", 7],
            alice, new_events=["contributor.registered"])
p = json.loads(ev[0]["payload_json"])
rows = mine(alice_id)
check("one row bound to alice's identity", len(rows) == 1, str(rows))
a = rows[0]
check("name normalised", a["name"] == "Alice Judge", a["name"])
check("devices deduped", a["devices"] == ["phone", "android_webxr"], str(a["devices"]))
check("online after sign-up", a["online"] is True)
check("payouts off, no stripe acct", a["payouts_enabled"] is False and a["stripe_account_id"] == [1, []])
check("event has ticket + no email", p["source_ticket_id"] == 7 and p["has_email"] is True and "example" not in ev[0]["payload_json"].lower())
contact = sql(f"SELECT * FROM contributor_contact WHERE contributor_id = {a['id']}")
check("email stored lowercased in private table", len(contact) == 1 and contact[0]["email"] == "alice@example.com")
code, text = anon_sql("SELECT * FROM contributor_contact", bob)
check("contributor_contact not readable by others", code != 200 or json.loads(text)[0]["rows"] == [], text[:200])
pres = sql(f"SELECT * FROM contributor_presence WHERE contributor_id = {a['id']}")
check("presence row from join", len(pres) == 1 and pres[0]["page"] == "join")
check("sweep schedule seeded once", len(sql("SELECT * FROM contributor_presence_sweep")) == 1)

# refresh / second call: same identity -> same row
expect("alice re-registers (refresh)", "register_contributor", ["Alice", "", ["phone"], "contributor", 7],
       alice, new_events=["contributor.updated"])
rows = mine(alice_id)
check("still one row, same id", len(rows) == 1 and rows[0]["id"] == a["id"])
check("email kept when blank", sql(f"SELECT * FROM contributor_contact WHERE contributor_id = {a['id']}")[0]["email"] == "alice@example.com")
expect("kind is fixed", "register_contributor", ["Alice", "", [], "dataset_seller", 0], alice, ok=False, err="kind is fixed")

ev = expect("bob joins anonymously", "register_contributor", ["", "", [], "dataset_seller", 0], bob,
            new_events=["contributor.registered"])
b = mine(bob_id)[0]
check("guest name from identity", b["name"] == "guest-" + bob_id.lower().removeprefix("0x")[:6], b["name"])
check("dataset_seller kind", b["kind"] == "dataset_seller")
check("no contact row for bob", sql(f"SELECT * FROM contributor_contact WHERE contributor_id = {b['id']}") == [])

# ---------------------------------------------------------------- presence
expect("heartbeat while online: no event", "contributor_heartbeat", ["marketplace"], alice, new_events=[])
check("page updated", sql(f"SELECT * FROM contributor_presence WHERE contributor_id = {a['id']}")[0]["page"] == "marketplace")
expect("bad page", "contributor_heartbeat", ["Market Place"], alice, ok=False, err="bad page")
expect("alice leaves", "contributor_offline", [], alice, new_events=["contributor.offline"])
check("alice offline", mine(alice_id)[0]["online"] is False)
expect("leave twice: no event", "contributor_offline", [], alice, new_events=[])
expect("heartbeat brings alice back", "contributor_heartbeat", ["teleop/3"], alice, new_events=["contributor.online"])
check("alice online", mine(alice_id)[0]["online"] is True)
check("still exactly one sweep schedule", len(sql("SELECT * FROM contributor_presence_sweep")) == 1)

if not A.skip_sweep:
    # Bob goes silent; alice keeps heartbeating. Stale after 75 s, sweep every 30 s.
    deadline = time.time() + 130
    while time.time() < deadline:
        call("contributor_heartbeat", ["marketplace"], alice)
        if mine(bob_id)[0]["online"] is False:
            break
        time.sleep(10)
    check("sweep marked silent bob offline", mine(bob_id)[0]["online"] is False)
    check("alice (heartbeating) still online", mine(alice_id)[0]["online"] is True)
    off = [e for e in events() if e["type"] == "contributor.offline" and json.loads(e["payload_json"])["contributor_id"] == b["id"]]
    check("stale offline logged by spacetime", len(off) == 1 and off[0]["actor"] == "spacetime"
          and json.loads(off[0]["payload_json"])["reason"] == "stale")

print(f"\nPASS {PASSED} checks")
