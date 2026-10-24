#!/usr/bin/env python3
"""TDM-9 reducer tests: register_robot, robot_heartbeat, create_ticket,
update_run_settings, set_ticket_status.

Runs against a THROWAWAY local server (it creates rows), stdlib only:

    spacetime start -l 127.0.0.1:3909 --data-dir $TMPDIR/tdm9-stdb/data --non-interactive
    cd spacetime && spacetime publish -s http://127.0.0.1:3909 tdm9test --yes
    set -a; source ../.env; set +a      # SPACETIME_TOKEN = publisher = owner (never printed)
    python3 tests/test_tdm9_ticket_reducers.py [--host http://127.0.0.1:3909] [--db tdm9test]

Checks: defaults applied, overrides merged, range validation, illegal status
transitions rejected, role rules (anonymous / mcp / robot_client), the eval
loop + max_iterations cap, and that every state-changing call appended exactly
the expected event_log row(s).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

P = argparse.ArgumentParser()
P.add_argument("--host", default="http://127.0.0.1:3909")
P.add_argument("--db", default="tdm9test")
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


def call(reducer: str, args: list, token: str | None = OWNER) -> tuple[bool, str]:
    code, text = _req(f"{BASE}/call/{reducer}", json.dumps(args).encode(), token, "application/json")
    return 200 <= code < 300, text


def sql(q: str) -> list[dict]:
    code, text = _req(f"{BASE}/sql", q.encode(), OWNER)
    assert code == 200, f"sql failed {code}: {text}"
    res = json.loads(text)[0]
    cols = [e["name"]["some"] for e in res["schema"]["elements"]]
    return [dict(zip(cols, row)) for row in res["rows"]]


def new_identity() -> tuple[str, str]:
    code, text = _req(f"{A.host}/v1/identity", b"", None)
    assert code == 200, text
    d = json.loads(text)
    return d["identity"], d["token"]


def events() -> list[dict]:
    return sorted(sql("SELECT * FROM event_log"), key=lambda r: r["id"])


PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if not cond:
        sys.exit(f"FAIL {name} {detail}")
    PASSED += 1
    print(f"ok   {name}")


def expect(name: str, reducer: str, args: list, *, token: str | None = OWNER, ok: bool = True,
           err: str = "", new_events: list[str] | None = None) -> list[dict]:
    """Call a reducer and assert success/failure plus the exact event types appended."""
    before = len(events())
    success, text = call(reducer, args, token)
    check(f"{name}: {'accepted' if ok else 'rejected'}", success == ok and (ok or err in text), text[:300])
    added = events()[before:]
    want = (new_events if new_events is not None else []) if ok else []
    check(f"{name}: event_log +{[e['type'] for e in added]}", [e["type"] for e in added] == want)
    return added


# ---------------------------------------------------------------- identities
mcp_id, mcp_tok = new_identity()
rc_id, rc_tok = new_identity()
rc2_id, rc2_tok = new_identity()
_, anon_tok = new_identity()
expect("register mcp identity", "register_service_identity", [[f"0x{mcp_id}"], "mcp", "test.tdm9 mcp"],
       new_events=["infra.service_identity_registered"])
expect("register robot_client identity", "register_service_identity", [[f"0x{rc_id}"], "robot_client", "test.tdm9 rc"],
       new_events=["infra.service_identity_registered"])
expect("register 2nd robot_client", "register_service_identity", [[f"0x{rc2_id}"], "robot_client", "test.tdm9 rc2"],
       new_events=["infra.service_identity_registered"])

# ---------------------------------------------------------------- robots
expect("anonymous register_robot", "register_robot", ["test.tdm9-yam", "i2rt_yam", "v0", "{}"],
       token=anon_tok, ok=False, err="unauthorized")
expect("register_robot bad camera json", "register_robot", ["test.tdm9-yam", "i2rt_yam", "v0", "[1]"],
       ok=False, err="camera_config_json")
expect("register_robot (owner)", "register_robot",
       ["test.tdm9-yam", "i2rt_yam", "v0", '{"wrist": "/dev/video0"}'], new_events=["robot.registered"])
robot = [r for r in sql("SELECT * FROM robot") if r["name"] == "test.tdm9-yam"][0]
rid = robot["id"]
check("robot starts offline", robot["status"] == "offline")
expect("re-register same name updates", "register_robot",
       ["test.tdm9-yam", "i2rt_yam", "v1", "{}"], token=rc_tok, new_events=["robot.updated"])
check("still one robot with that name", len([r for r in sql("SELECT * FROM robot") if r["name"] == "test.tdm9-yam"]) == 1)
check("adapter_version updated", sql(f"SELECT * FROM robot WHERE id = {rid}")[0]["adapter_version"] == "v1")
expect("heartbeat bad status", "robot_heartbeat", [rid, "dancing"], token=rc_tok, ok=False, err="status must be one of")
expect("heartbeat online (bound client)", "robot_heartbeat", [rid, "online"], token=rc_tok,
       new_events=["robot.status_changed"])
expect("heartbeat same status: no event", "robot_heartbeat", [rid, "online"], token=rc_tok, new_events=[])
expect("heartbeat from other robot_client", "robot_heartbeat", [rid, "busy"], token=rc2_tok,
       ok=False, err="bound to another")
expect("heartbeat from mcp", "robot_heartbeat", [rid, "busy"], token=mcp_tok, ok=False, err="unauthorized")
expect("heartbeat unknown robot", "robot_heartbeat", [999999, "online"], token=rc_tok, ok=False, err="unknown robot")

# ---------------------------------------------------------------- create_ticket
expect("anonymous create_ticket", "create_ticket", [rid, "cups", 0.8, 5000, ""],
       token=anon_tok, ok=False, err="unauthorized")
expect("create_ticket unknown robot", "create_ticket", [999999, "cups", 0.8, 5000, ""], ok=False, err="unknown robot")
expect("create_ticket target 0", "create_ticket", [rid, "cups", 0.0, 5000, ""], ok=False, err="target_success")
expect("create_ticket unknown override key", "create_ticket", [rid, "cups", 0.8, 5000, '{"foo": 1}'],
       ok=False, err="unknown run setting")
expect("create_ticket budget override refused", "create_ticket", [rid, "cups", 0.8, 5000, '{"budget_cents": 1}'],
       ok=False, err="budget_cents")
expect("create_ticket eval_trials 51", "create_ticket", [rid, "cups", 0.8, 5000, '{"eval_trials": 51}'],
       ok=False, err="eval_trials")

ev = expect("create_ticket defaults (mcp)", "create_ticket",
            [rid, "test.tdm9 put cups in the bin", 0.8, 5000, ""], token=mcp_tok, new_events=["ticket.created"])
tid = json.loads(ev[0]["payload_json"])["ticket_id"]
check("event actor is caller role", ev[0]["actor"] == "mcp")
tk = sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]
check("ticket status created, iteration 0", tk["status"] == "created" and tk["iteration"] == 0)
check("ticket created_by mcp", tk["created_by"] == "mcp")
ds = sql(f"SELECT * FROM dataset WHERE ticket_id = {tid}")
check("dataset row exists and is linked", len(ds) == 1 and tk["dataset_id"] == ds[0]["id"])
rs = sql(f"SELECT * FROM run_settings WHERE ticket_id = {tid}")[0]
check("defaults: eval_trials=10", rs["eval_trials"] == 10)
check("defaults: success_threshold=0.8", rs["success_threshold"] == 0.8)
check("defaults: require_ci_low=false", rs["require_ci_low"] is False)
check("defaults: target_episodes=5, max_iterations=3", rs["target_episodes"] == 5 and rs["max_iterations"] == 3)
check("defaults: budget mirrors ticket", rs["budget_cents"] == 5000)
hp = json.loads(rs["hyperparams_json"])
check("defaults: hyperparams", hp == {"lr": 2.5e-5, "steps": 2000, "batch_size": 32, "lora_rank": 16, "prompt": ""}, str(hp))

ev = expect("create_ticket with overrides", "create_ticket",
            [rid, "test.tdm9 overrides", 0.7, 2000,
             '{"eval_trials": 6, "require_ci_low": true, "hyperparams": {"steps": 500, "prompt": "pick cup"}}'],
            new_events=["ticket.created"])
tid2 = json.loads(ev[0]["payload_json"])["ticket_id"]
rs2 = sql(f"SELECT * FROM run_settings WHERE ticket_id = {tid2}")[0]
hp2 = json.loads(rs2["hyperparams_json"])
check("overrides applied", rs2["eval_trials"] == 6 and rs2["require_ci_low"] is True)
check("threshold defaults to target_success", rs2["success_threshold"] == 0.7)
check("hyperparams merged", hp2["steps"] == 500 and hp2["prompt"] == "pick cup" and hp2["lr"] == 2.5e-5, str(hp2))

# ---------------------------------------------------------------- update_run_settings
expect("update eval_trials 0", "update_run_settings", [tid, '{"eval_trials": 0}'], ok=False, err="eval_trials")
expect("update eval_trials 2.5", "update_run_settings", [tid, '{"eval_trials": 2.5}'], ok=False, err="eval_trials")
expect("update threshold 1.5", "update_run_settings", [tid, '{"success_threshold": 1.5}'], ok=False, err="success_threshold")
expect("update budget -1", "update_run_settings", [tid, '{"budget_cents": -1}'], ok=False, err="budget_cents")
expect("update unknown hyperparam", "update_run_settings", [tid, '{"hyperparams": {"momentum": 0.9}}'],
       ok=False, err="unknown hyperparam")
expect("update empty patch", "update_run_settings", [tid, "{}"], ok=False, err="empty")
expect("update not json", "update_run_settings", [tid, "{nope"], ok=False, err="not valid JSON")
expect("update anonymous", "update_run_settings", [tid, '{"eval_trials": 20}'], token=anon_tok, ok=False, err="unauthorized")
expect("update valid", "update_run_settings",
       [tid, '{"eval_trials": 20, "success_threshold": 0.9, "budget_cents": 7500, "hyperparams": {"lr": 1e-4}}'],
       token=mcp_tok, new_events=["ticket.run_settings_updated"])
rs = sql(f"SELECT * FROM run_settings WHERE ticket_id = {tid}")[0]
check("update applied", rs["eval_trials"] == 20 and rs["success_threshold"] == 0.9 and rs["budget_cents"] == 7500)
check("hyperparams patch merged", json.loads(rs["hyperparams_json"]) ==
      {"lr": 1e-4, "steps": 2000, "batch_size": 32, "lora_rank": 16, "prompt": ""})
check("ticket.budget_cents mirrored", sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]["budget_cents"] == 7500)

# ---------------------------------------------------------------- set_ticket_status
expect("illegal created -> train", "set_ticket_status", [tid, "train", "skip"], ok=False, err="illegal ticket transition")
expect("illegal created -> shipped", "set_ticket_status", [tid, "shipped", ""], ok=False, err="illegal ticket transition")
expect("unknown status", "set_ticket_status", [tid, "diagnosing", ""], ok=False, err="status must be one of")
expect("mcp may not advance", "set_ticket_status", [tid, "onboarding", ""], token=mcp_tok, ok=False, err="unauthorized")
expect("anonymous may not advance", "set_ticket_status", [tid, "onboarding", ""], token=anon_tok, ok=False, err="unauthorized")
for frm, to in [("created", "onboarding"), ("onboarding", "baseline"), ("baseline", "diagnose"),
                ("diagnose", "plan_data"), ("plan_data", "dispatch"), ("dispatch", "review"),
                ("review", "train"), ("train", "eval")]:
    ev = expect(f"{frm} -> {to}", "set_ticket_status", [tid, to, f"test.tdm9 {frm}->{to}"],
                new_events=["ticket.status_changed"])
    p = json.loads(ev[0]["payload_json"])
    check(f"{frm} -> {to}: payload", p["from"] == frm and p["to"] == to and p["note"].startswith("test.tdm9"))
expect("illegal eval -> train", "set_ticket_status", [tid, "train", ""], ok=False, err="illegal ticket transition")
expect("loop eval -> dispatch (iter 1)", "set_ticket_status", [tid, "dispatch", "more data"],
       new_events=["ticket.status_changed"])
check("iteration bumped to 1", sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]["iteration"] == 1)
for to in ["review", "train", "eval"]:
    expect(f"iter1 -> {to}", "set_ticket_status", [tid, to, ""], new_events=["ticket.status_changed"])
expect("loop eval -> plan_data (iter 2)", "set_ticket_status", [tid, "plan_data", ""], new_events=["ticket.status_changed"])
expect("update max_iterations <= iteration", "update_run_settings", [tid, '{"max_iterations": 2}'],
       ok=False, err="max_iterations")
for to in ["dispatch", "review", "train", "eval"]:
    expect(f"iter2 -> {to}", "set_ticket_status", [tid, to, ""], new_events=["ticket.status_changed"])
expect("loop past max_iterations=3", "set_ticket_status", [tid, "diagnose", ""], ok=False, err="max_iterations")
expect("eval -> shipped", "set_ticket_status", [tid, "shipped", "target met"], new_events=["ticket.status_changed"])
expect("shipped is terminal", "set_ticket_status", [tid, "failed", ""], ok=False, err="illegal ticket transition")
expect("settings frozen after ship", "update_run_settings", [tid, '{"eval_trials": 5}'], ok=False, err="frozen")

expect("mcp may cancel", "set_ticket_status", [tid2, "cancelled", "user cancelled"], token=mcp_tok,
       new_events=["ticket.status_changed"])
expect("cancelled is terminal", "set_ticket_status", [tid2, "onboarding", ""], ok=False, err="illegal ticket transition")

print(f"\nALL {PASSED} CHECKS PASSED")
