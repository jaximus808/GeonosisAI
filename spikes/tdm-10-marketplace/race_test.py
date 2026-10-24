"""TDM-10: claim_task race and lease tests against a LOCAL throwaway SpacetimeDB.

Run (from the repo root):
    W=$TMPDIR/tdm10
    spikes/tdm-10-marketplace/build_test_module.sh $W/module
    spacetime start --listen-addr 127.0.0.1:3077 --data-dir $W/data &
    (cd $W/module && spacetime publish -s http://127.0.0.1:3077 tdm10test --module-path . --anonymous --yes --delete-data=always)
    uv run --no-project --with websockets python spikes/tdm-10-marketplace/race_test.py

In the test module only, lease TTLs are shortened to 3 s for arm control and 5 s
for resets (build_test_module.sh). Every identity is minted fresh from the
local server, so no owner token or .env is used.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import websockets

HOST = os.environ.get("TDM10_HOST", "http://127.0.0.1:3077")
DB = os.environ.get("TDM10_DB", "tdm10test")
BASE = f"{HOST}/v1/database/{DB}"
WS_URL = BASE.replace("http://", "ws://") + "/subscribe"
CONTROL_TTL_S, RESET_TTL_S = 3, 5
RACES_WS, RACES_HTTP = 100, 30

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        failures.append(what)


def new_identity() -> tuple[str, str]:
    req = urllib.request.Request(f"{HOST}/v1/identity", data=b"", method="POST")
    with urllib.request.urlopen(req) as r:
        d = json.loads(r.read())
    return d["identity"], d["token"]


def call(token: str | None, reducer: str, args: list) -> tuple[bool, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BASE}/call/{reducer}", data=json.dumps(args).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return True, r.read().decode()
    except urllib.error.HTTPError as e:
        return False, e.read().decode()


def sql(token: str, q: str) -> list[dict]:
    req = urllib.request.Request(f"{BASE}/sql", data=q.encode(), headers={"Authorization": f"Bearer {token}"}, method="POST")
    with urllib.request.urlopen(req) as r:
        res = json.loads(r.read())[-1]
    cols = [e["name"]["some"] if isinstance(e["name"], dict) else e["name"] for e in res["schema"]["elements"]]
    return [dict(zip(cols, row)) for row in res["rows"]]


def opt(v):
    """SATS option over HTTP SQL: {"some": x} | {"none": []} (or [tag, val])."""
    if isinstance(v, dict):
        return v.get("some")
    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], int):
        return None if v[0] == 1 else v[1]
    return v


def micros(v) -> int | None:
    if not (isinstance(v, dict) and "__timestamp_micros_since_unix_epoch__" in v):
        v = opt(v)
    if v is None:
        return None
    if isinstance(v, dict):
        return int(v["__timestamp_micros_since_unix_epoch__"])
    if isinstance(v, list):
        return int(v[0])
    return int(v)


def task(tok: str, task_id: int) -> dict:
    return sql(tok, f"SELECT * FROM task WHERE id = {task_id}")[0]


def events(tok: str, type_: str, task_id: int) -> list[dict]:
    rows = sql(tok, f"SELECT * FROM event_log WHERE type = '{type_}'")
    return [r for r in rows if json.loads(r["payload_json"]).get("task_id") == task_id]


def create_task(orch_tok: str, ticket_id: int, type_: str, payload: dict | None = None) -> int:
    ok, msg = call(orch_tok, "create_task", [ticket_id, type_, 0, json.dumps(payload or {"instructions": "test.tdm10"})])
    assert ok, msg
    return max(int(r["id"]) for r in sql(orch_tok, "SELECT id FROM task"))


class WsClient:
    """One persistent WebSocket per identity; CallReducer + wait for its TransactionUpdate."""

    def __init__(self, token: str):
        self.token, self.ws, self.rid = token, None, 0
        self.pending: dict[int, asyncio.Future] = {}

    async def open(self):
        self.ws = await websockets.connect(WS_URL, subprotocols=["v1.json.spacetimedb"],
                                           additional_headers={"Authorization": f"Bearer {self.token}"}, max_size=None)
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            (kind, body), = msg.items()
            if kind in ("TransactionUpdate",):
                rid = (body.get("reducer_call") or {}).get("request_id")
                fut = self.pending.pop(rid, None)
                if fut and not fut.done():
                    st = body["status"]
                    fut.set_result(("Committed" in st, st.get("Failed"), body.get("timestamp")))

    def frame(self, reducer: str, args: list) -> tuple[asyncio.Future, str]:
        self.rid += 1
        fut = self.pending[self.rid] = asyncio.get_running_loop().create_future()
        return fut, json.dumps({"CallReducer": {"reducer": reducer, "args": json.dumps(args), "request_id": self.rid, "flags": 0}})

    async def close(self):
        await self.ws.close()


async def ws_races(orch, a_tok, b_tok, a_id, b_id, ticket_id):
    print(f"\n[1] WebSocket race: {RACES_WS} trials, two identities send claim_task on the same task back to back")
    A, B = WsClient(a_tok), WsClient(b_tok)
    await A.open(); await B.open()
    await asyncio.sleep(0.3)
    wins = {"A": 0, "B": 0}
    max_send_gap_us = 0
    max_server_gap_us = 0
    bad = 0
    types = ["clean_teleop", "recovery_takeover", "reset"]
    for i in range(RACES_WS):
        tid = create_task(orch, ticket_id, types[i % 3])
        ra, fa = A.frame("claim_task", [tid, a_id])
        rb, fb = B.frame("claim_task", [tid, b_id])
        first, second = ((A, fa), (B, fb)) if i % 2 == 0 else ((B, fb), (A, fa))
        t0 = time.perf_counter_ns()
        s1 = first[0].ws.send(first[1]); s2 = second[0].ws.send(second[1])
        await asyncio.gather(s1, s2)
        max_send_gap_us = max(max_send_gap_us, (time.perf_counter_ns() - t0) // 1000)
        res_a = await asyncio.wait_for(ra, 10)
        res_b = await asyncio.wait_for(rb, 10)
        ta, tb = micros(res_a[2]), micros(res_b[2])
        if ta is None or tb is None:
            bad += 1
            print(f"  trial {i}: missing reducer timestamp A={res_a} B={res_b}")
        else:
            max_server_gap_us = max(max_server_gap_us, abs(ta - tb))
        n_ok = int(res_a[0]) + int(res_b[0])
        row = task(orch, tid)
        winner = "A" if res_a[0] else "B"
        loser_err = res_b[1] if res_a[0] else res_a[1]
        claimed_events = events(orch, "task.claimed", tid)
        good = (n_ok == 1 and row["status"] == "claimed"
                and int(row["assignee_id"]) == (a_id if winner == "A" else b_id)
                and len(claimed_events) == 1 and loser_err and "already claimed" in loser_err)
        if not good:
            bad += 1
            print(f"  trial {i}: n_ok={n_ok} row={row} A={res_a} B={res_b} events={len(claimed_events)}")
        wins[winner] += 1
        # winner hands the arm back so the next control task on this robot is claimable
        winner_tok = a_tok if winner == "A" else b_tok
        ok, msg = call(winner_tok, "complete_task", [tid, json.dumps({"trial": i})])
        assert ok, msg
    check(bad == 0, f"exactly one claim succeeded in every trial ({RACES_WS - bad}/{RACES_WS}); loser got 'already claimed'; exactly one task.claimed event")
    print(f"  wins A={wins['A']} B={wins['B']}; max client send gap {max_send_gap_us} us; "
          f"max server reducer-timestamp gap {max_server_gap_us} us")
    check(max_server_gap_us < 1000, "both reducer calls of each pair were executed within the same millisecond on the server")
    await A.close(); await B.close()


def http_races(orch, a_tok, b_tok, a_id, b_id, ticket_id):
    print(f"\n[2] HTTP race: {RACES_HTTP} trials, two threads released by one barrier")
    bad = 0
    for i in range(RACES_HTTP):
        tid = create_task(orch, ticket_id, "clean_teleop")
        barrier = threading.Barrier(2)
        out = {}

        def go(name, tok, cid):
            barrier.wait()
            out[name] = call(tok, "claim_task", [tid, cid])

        ts = [threading.Thread(target=go, args=("A", a_tok, a_id)), threading.Thread(target=go, args=("B", b_tok, b_id))]
        [t.start() for t in ts]; [t.join() for t in ts]
        n_ok = int(out["A"][0]) + int(out["B"][0])
        if n_ok != 1 or len(events(orch, "task.claimed", tid)) != 1:
            bad += 1
            print(f"  trial {i}: {out}")
        winner_tok = a_tok if out["A"][0] else b_tok
        assert call(winner_tok, "complete_task", [tid, "{}"])[0]
    check(bad == 0, f"exactly one HTTP claim succeeded in every trial ({RACES_HTTP - bad}/{RACES_HTTP})")


def lease_and_auth(orch, a_tok, b_tok, x_tok, a_id, b_id, ticket_id, estop_ticket_id):
    print("\n[3] Lease, auth and state-machine checks")
    t1 = create_task(orch, ticket_id, "clean_teleop")

    ok, msg = call(b_tok, "claim_task", [t1, a_id])
    check(not ok and "unauthorized" in msg, "B cannot claim on behalf of contributor A")
    ok, msg = call(None, "claim_task", [t1, a_id])
    check(not ok, "anonymous caller cannot claim as A")
    ok, msg = call(a_tok, "create_task", [ticket_id, "reset", 0, "{}"])
    check(not ok and "unauthorized" in msg, "a contributor cannot create_task")

    ok, msg = call(a_tok, "claim_task", [t1, a_id]); check(ok, f"A claims t{t1}")
    r = task(orch, t1)
    ttl = micros(r["lease_expires_at"]) - micros(r["claimed_at"])
    check(ttl == CONTROL_TTL_S * 1_000_000, f"control lease = claimed_at + {CONTROL_TTL_S}s (test TTL)")
    gen = micros(r["claimed_at"])

    ok, msg = call(b_tok, "claim_task", [t1, b_id]); check(not ok and "already claimed" in msg, "B cannot claim A's live lease")
    for red, args in (("heartbeat_claim", [t1]), ("release_task", [t1]), ("complete_task", [t1, "{}"])):
        ok, msg = call(b_tok, red, args); check(not ok and "unauthorized" in msg, f"B cannot {red} A's task")
        ok, msg = call(None, red, args); check(not ok, f"anonymous cannot {red}")

    t2 = create_task(orch, ticket_id, "recovery_takeover")
    ok, msg = call(b_tok, "claim_task", [t2, b_id])
    check(not ok and "already under control" in msg, "one arm, one operator: B cannot claim a second control task on the same robot")
    t3 = create_task(orch, ticket_id, "reset")
    ok, msg = call(b_tok, "claim_task", [t3, b_id]); check(ok, "a reset task on the same robot is still claimable")
    r3 = task(orch, t3)
    check(micros(r3["lease_expires_at"]) - micros(r3["claimed_at"]) == RESET_TTL_S * 1_000_000, f"reset lease = {RESET_TTL_S}s (test TTL)")

    time.sleep(1.5)
    before = micros(task(orch, t1)["lease_expires_at"])
    ok, msg = call(a_tok, "heartbeat_claim", [t1]); check(ok, "A heartbeats")
    r = task(orch, t1)
    check(micros(r["lease_expires_at"]) > before and micros(r["claimed_at"]) == gen, "heartbeat extends lease, claim generation (claimed_at) unchanged")
    ok, msg = call(a_tok, "claim_task", [t1, a_id]); check(ok, "A re-claiming its own live lease refreshes it (page reload)")
    check(len(events(orch, "task.claim_refreshed", t1)) == 1 and micros(task(orch, t1)["claimed_at"]) == gen, "refresh logged, generation unchanged")

    print(f"  ... letting A's lease lapse ({CONTROL_TTL_S}s TTL, no heartbeat)")
    time.sleep(CONTROL_TTL_S + 0.5)
    ok, msg = call(a_tok, "heartbeat_claim", [t1]); check(not ok and "expired" in msg, "a lapsed lease cannot be revived by heartbeat")
    ok, msg = call(a_tok, "complete_task", [t1, "{}"]); check(not ok and "expired" in msg, "holder cannot complete on a lapsed lease")
    ok, msg = call(b_tok, "claim_task", [t2, b_id]); check(ok, "after lapse, robot is free: B claims the other control task")
    ok, msg = call(b_tok, "release_task", [t2]); check(ok, "B releases t2")
    r2 = task(orch, t2)
    check(r2["status"] == "open" and int(r2["assignee_id"]) == 0 and opt(r2["lease_expires_at"]) is None, "release -> open, no assignee, no lease")
    ok, msg = call(b_tok, "claim_task", [t1, b_id]); check(ok, "B re-claims A's expired task t1")
    r = task(orch, t1)
    check(int(r["assignee_id"]) == b_id and micros(r["claimed_at"]) != gen, "t1 now held by B with a new claim generation")
    exp = events(orch, "task.lease_expired", t1)
    check(len(exp) == 1 and json.loads(exp[0]["payload_json"])["previous_assignee_id"] == a_id, "task.lease_expired logged with previous assignee A")
    ok, msg = call(a_tok, "heartbeat_claim", [t1]); check(not ok and "unauthorized" in msg, "A (old holder) cannot heartbeat B's new claim")

    ok, msg = call(b_tok, "complete_task", [t1, "not json"]); check(not ok and "JSON" in msg, "complete_task rejects invalid result_json")
    ok, msg = call(b_tok, "complete_task", [t1, json.dumps({"episode_uri": "test://ep"})]); check(ok, "B completes t1")
    r = task(orch, t1)
    check(r["status"] == "completed" and opt(r["lease_expires_at"]) is None and micros(r["completed_at"]), "completed: lease cleared, completed_at set")
    ok, msg = call(a_tok, "claim_task", [t1, a_id]); check(not ok and "completed" in msg, "completed task cannot be claimed")

    # sweeper: let B's reset lease lapse, then any caller sweeps it
    print(f"  ... letting reset lease lapse ({RESET_TTL_S}s TTL) for the sweeper")
    time.sleep(RESET_TTL_S)
    ok, msg = call(x_tok, "expire_task_leases", []); check(ok, "any identity may run expire_task_leases")
    r3 = task(orch, t3)
    check(r3["status"] == "open" and int(r3["assignee_id"]) == 0, "sweeper wrote the lapsed reset task back to open")
    check(len(events(orch, "task.lease_expired", t3)) == 1, "sweeper logged task.lease_expired")

    # cancel
    ok, msg = call(a_tok, "claim_task", [t2, a_id]); check(ok, "A claims t2")
    ok, msg = call(a_tok, "cancel_task", [t2, "x"]); check(not ok and "unauthorized" in msg, "contributor cannot cancel")
    ok, msg = call(orch, "cancel_task", [t2, "test.tdm10 cancel"]); check(ok, "orchestrator cancels a claimed task (revokes the claim)")
    ok, msg = call(a_tok, "heartbeat_claim", [t2]); check(not ok and "cancelled" in msg, "heartbeat on cancelled task fails")
    ok, msg = call(b_tok, "claim_task", [t2, b_id]); check(not ok and "cancelled" in msg, "cancelled task cannot be claimed")

    # admin on-behalf claim and estop guard
    t4 = create_task(orch, ticket_id, "reset")
    ok, msg = call(orch, "claim_task", [t4, a_id]); check(ok, "orchestrator may claim on a contributor's behalf")
    ok, msg = call(a_tok, "heartbeat_claim", [t4]); check(ok, "the contributor can then heartbeat it")
    t5 = create_task(orch, estop_ticket_id, "clean_teleop")
    ok, msg = call(a_tok, "claim_task", [t5, a_id]); check(not ok and "estop" in msg, "control task on an estopped robot cannot be claimed")

    # create_task validation
    ok, msg = call(orch, "create_task", [ticket_id, "dance", 0, "{}"]); check(not ok, "create_task rejects an unknown type")
    ok, msg = call(orch, "create_task", [ticket_id, "reset", 0, "[1]"]); check(not ok, "create_task rejects non-object payload_json")
    ok, msg = call(orch, "create_task", [ticket_id, "reset", 0, json.dumps({"data_plan_id": 999})]); check(not ok and "data_plan" in msg, "create_task rejects an unknown data_plan_id")
    ok, msg = call(orch, "create_task", [999, "reset", 0, "{}"]); check(not ok, "create_task rejects an unknown ticket")
    r = task(orch, create_task(orch, ticket_id, "clean_teleop"))
    check(int(r["price_cents"]) == 1000 and r["instructions"] == "test.tdm10", "price 0 -> default (clean_teleop 1000c); instructions stored")


def event_log_coverage(orch):
    print("\n[4] event_log coverage")
    rows = sql(orch, "SELECT * FROM event_log")
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    for t_ in ("task.created", "task.claimed", "task.claim_refreshed", "task.heartbeat", "task.released",
               "task.completed", "task.cancelled", "task.lease_expired"):
        check(by_type.get(t_, 0) > 0, f"{t_}: {by_type.get(t_, 0)} rows")
    created = len(sql(orch, "SELECT id FROM task"))
    check(by_type.get("task.created") == created, f"one task.created per task ({created})")


def main():
    orch_id, orch = new_identity()
    a_ident, a_tok = new_identity()
    b_ident, b_tok = new_identity()
    _, x_tok = new_identity()
    assert call(orch, "tdm_10_test_grant", ["orchestrator"])[0]
    assert call(a_tok, "tdm_10_test_add_contributor", ["alice"])[0]
    assert call(b_tok, "tdm_10_test_add_contributor", ["bob"])[0]
    assert call(orch, "tdm_10_test_seed_ticket", ["online"])[0]
    assert call(orch, "tdm_10_test_seed_ticket", ["estop"])[0]
    contribs = {r["name"]: int(r["id"]) for r in sql(orch, "SELECT * FROM contributor")}
    a_id, b_id = contribs["alice"], contribs["bob"]
    tickets = sorted(int(r["id"]) for r in sql(orch, "SELECT id FROM ticket"))
    ticket_id, estop_ticket_id = tickets[-2], tickets[-1]
    print(f"contributors alice={a_id} bob={b_id}; ticket {ticket_id} (robot online), ticket {estop_ticket_id} (robot estop)")

    asyncio.run(ws_races(orch, a_tok, b_tok, a_id, b_id, ticket_id))
    http_races(orch, a_tok, b_tok, a_id, b_id, ticket_id)
    lease_and_auth(orch, a_tok, b_tok, x_tok, a_id, b_id, ticket_id, estop_ticket_id)
    event_log_coverage(orch)

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
