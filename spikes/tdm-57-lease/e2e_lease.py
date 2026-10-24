"""TDM-57 end-to-end proof of exclusive control on the local stack (stack.py + mock_control.py).

Two contributor identities (A, B) drive the real TDM-10 reducers on the throwaway DB, get tokens from
the real TDM-55 /stream/token route, and talk to the [MOCK] /control server, which gates commands
with the real rdf_robot_client.tdm57_lease guard. Measures how fast the robot side stops accepting.

    cd services/robot-client && uv run python ../../spikes/tdm-57-lease/e2e_lease.py
Writes spikes/tdm-57-lease/tmp/e2e_result.json.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

import httpx
import websockets

HOST, DB = "http://127.0.0.1:3057", "tdm57lease"
API, CONTROL = "http://127.0.0.1:8857", "ws://127.0.0.1:8957/control"
RUN = Path(tempfile.gettempdir()) / "tdm-57" / "run-3057"
OUT = Path(__file__).parent / "tmp" / "e2e_result.json"
results: dict = {}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Who:
    def __init__(self, name: str):
        r = httpx.post(f"{HOST}/v1/identity")
        r.raise_for_status()
        d = r.json()
        self.name, self.identity, self.token = name, d["identity"], d["token"]
        self.http = httpx.Client(headers={"Authorization": f"Bearer {self.token}"}, timeout=10)
        ok, msg = self.call("register_contributor", name, "", ["laptop"], "contributor", 0)
        assert ok, msg
        self.cid = int(next(r for r in sql_dicts("SELECT * FROM contributor") if r["name"] == name)["id"])

    def call(self, reducer: str, *args) -> tuple[bool, str]:
        r = self.http.post(f"{HOST}/v1/database/{DB}/call/{reducer}", json=list(args))
        return r.status_code < 300, r.text.strip()

    def stream_token(self, task_id: int) -> tuple[int, dict]:
        r = httpx.post(f"{API}/stream/token", json={"task_id": task_id, "identity": self.identity,
                                                    "spacetime_token": self.token}, timeout=15)
        return r.status_code, r.json()


def sql(q: str) -> list[list]:
    r = httpx.post(f"{HOST}/v1/database/{DB}/sql", content=q.encode(), timeout=10)
    r.raise_for_status()
    return r.json()[-1]["rows"]


def sql_dicts(q: str) -> list[dict]:
    r = httpx.post(f"{HOST}/v1/database/{DB}/sql", content=q.encode(), timeout=10)
    r.raise_for_status()
    res = r.json()[-1]
    names = [e["name"]["some"] for e in res["schema"]["elements"]]
    return [dict(zip(names, row)) for row in res["rows"]]


def task_row(tid: int) -> dict:
    return sql_dicts(f"SELECT * FROM task WHERE id = {tid}")[0]


def opt_ts(v) -> int:
    # option<timestamp> in SQL JSON: [0, [micros]] = some, [1, []] = none
    if isinstance(v, list) and v and v[0] == 0:
        inner = v[1]
        return int(inner[0] if isinstance(inner, list) else inner)
    return 0


class Driver:
    """A browser stand-in: connects to /control and sends stamped 30 Hz cmds until told to stop."""

    def __init__(self, who: Who, task_id: int, grant: dict):
        self.who, self.task_id, self.grant = who, task_id, grant
        self.acks = self.rejects = 0
        self.lost: dict | None = None
        self.lost_at: float | None = None
        self.close_code: int | None = None
        self.first_reject_at: float | None = None
        self.auth: dict | None = None

    async def run(self, seconds: float, gen_override: int | None = None) -> None:
        async with websockets.connect(CONTROL) as ws:
            await ws.send(json.dumps({"type": "auth", "token": self.grant["token"]}))
            first = json.loads(await ws.recv())
            if first.get("t") != "auth_ok":
                self.lost, self.lost_at = first, time.time()
                try:
                    await ws.recv()
                except websockets.ConnectionClosed as e:
                    self.close_code = e.rcvd.code if e.rcvd else None
                return
            self.auth = first
            gen = gen_override if gen_override is not None else self.grant["claimed_at_us"]
            seq = 0

            async def reader():
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("t") == "ack":
                        self.acks += 1
                    elif m.get("t") == "rejected":
                        self.rejects += 1
                        self.first_reject_at = self.first_reject_at or time.time()
                    elif m.get("t") == "lease_lost":
                        self.lost, self.lost_at = m, time.time()

            rt = asyncio.create_task(reader())
            t_end = time.time() + seconds
            try:
                while time.time() < t_end and self.lost is None:
                    seq += 1
                    await ws.send(json.dumps({"t": "cmd", "seq": seq, "ts_ms": int(time.time() * 1000), "clutch": True,
                                              "d_pos": [0, 0, 0.001], "d_rot": [0, 0, 0], "gripper": 0.5,
                                              "speed_scale": 0.5, "task_id": self.task_id, "claimed_at_us": gen}))
                    await asyncio.sleep(1 / 30)
                await asyncio.sleep(0.3)
            except websockets.ConnectionClosed:
                pass
            finally:
                try:
                    await asyncio.wait_for(rt, 3)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    rt.cancel()
                self.close_code = ws.close_code


def check(name: str, ok: bool, **detail) -> None:
    results[name] = {"ok": bool(ok), **detail}
    log(f"{'PASS' if ok else 'FAIL'} {name} {detail}")


async def main() -> None:
    key = (RUN / "operator.key").read_text().strip()
    seed = json.loads((RUN / "seed.json").read_text())
    t1, t2 = seed["task_ids"][:2]
    a, b = Who("test.tdm57-alice"), Who("test.tdm57-bob")
    log(f"contributors A={a.cid} B={b.cid}; tasks {t1},{t2}")

    def revoke(tid: int, cancel: bool = False):
        return httpx.post(f"{API}/tasks/{tid}/revoke", json={"reason": "test.tdm57 revoke", "cancel": cancel},
                          headers={"X-RDF-Operator-Key": key}, timeout=10)

    # 1. Exclusive claim
    ok_a, _ = a.call("claim_task", t1, a.cid)
    ok_b, msg_b = b.call("claim_task", t1, b.cid)
    ok_b2, msg_b2 = b.call("claim_task", t2, b.cid)
    check("exclusive_claim", ok_a and not ok_b and not ok_b2, b_same_task=msg_b[:90], b_other_task=msg_b2[:90])

    # 2. Token only for the live claimant, bound to the generation
    sa, ga = a.stream_token(t1)
    sb, gb = b.stream_token(t1)
    gen = opt_ts(task_row(t1)["claimed_at"])
    check("token_claimant_only", sa == 200 and sb == 403 and ga.get("claimed_at_us") == gen,
          a_status=sa, b_status=sb, b_detail=gb.get("detail"), token_gen_matches=ga.get("claimed_at_us") == gen)

    # 3. Wrong generation in the message is refused; B's ops-less attempt is refused at admit
    d = Driver(a, t1, ga)
    await d.run(0.5, gen_override=gen + 1)
    check("stale_generation_message_rejected", d.acks == 0 and d.rejects > 0, rejects=d.rejects)

    # 4. Lease lapse: A heartbeats once, then stops (tab hidden). Robot must stop at lease expiry.
    ok_hb, _ = a.call("heartbeat_claim", t1)
    exp_us = opt_ts(task_row(t1)["lease_expires_at"])
    ga = a.stream_token(t1)[1]
    log(f"A heartbeat ok={ok_hb}; lease expires in {exp_us / 1e6 - time.time():.1f}s; driving until it lapses")
    d = Driver(a, t1, ga)
    # The token itself lives <= 60 s; keep the socket and ride it to expiry.
    await d.run(exp_us / 1e6 - time.time() + 6)
    lag = (d.lost_at or 0) - exp_us / 1e6
    check("lapse_stops_robot", d.lost is not None and d.lost.get("code") in ("lease_expired", "token_expired")
          and -0.5 < lag < 2.5 and d.close_code == 4004, code=(d.lost or {}).get("code"),
          seconds_after_expiry=round(lag, 2), acks=d.acks, close=d.close_code)

    # 5. Another claimant after the lapse; A's old token can't get back in
    ok_b, msg = b.call("claim_task", t1, b.cid)
    d_old = Driver(a, t1, ga)
    await d_old.run(0.5)
    sa2, _ = a.stream_token(t1)
    check("other_claimant_after_lapse", ok_b and d_old.auth is None and sa2 == 403,
          a_old_token=(d_old.lost or {}).get("code") or "token rejected", a_new_token_status=sa2)

    # 6. Release by the holder: B drives, releases; robot stops within the re-check window
    gb = b.stream_token(t1)[1]
    d = Driver(b, t1, gb)
    run = asyncio.create_task(d.run(10))
    await asyncio.sleep(1.5)
    t_rel = time.time()
    b.call("release_task", t1)
    await run
    check("release_stops_robot", d.lost is not None and d.lost.get("code") == "released"
          and (d.lost_at - t_rel) <= 2.6, seconds=round((d.lost_at or 0) - t_rel, 2), acks=d.acks)

    # 7. Operator revoke (release) while A drives, then reclaim works
    a.call("claim_task", t1, a.cid)
    d = Driver(a, t1, a.stream_token(t1)[1])
    run = asyncio.create_task(d.run(10))
    await asyncio.sleep(1.5)
    t_rev = time.time()
    r = revoke(t1)
    await run
    check("operator_revoke_release", r.status_code == 200 and d.lost is not None and d.lost.get("code") == "released"
          and (d.lost_at - t_rev) <= 2.6, http=r.status_code, seconds=round((d.lost_at or 0) - t_rev, 2))

    ok_re, _ = a.call("claim_task", t1, a.cid)
    d = Driver(a, t1, a.stream_token(t1)[1])
    run = asyncio.create_task(d.run(10))
    await asyncio.sleep(1.5)
    t_rev = time.time()
    r = revoke(t1, cancel=True)
    await run
    st, body = a.stream_token(t1)
    check("reclaim_then_operator_cancel", ok_re and r.status_code == 200 and d.lost is not None
          and d.lost.get("code") == "revoked" and (d.lost_at - t_rev) <= 2.6 and st == 403,
          seconds=round((d.lost_at or 0) - t_rev, 2), token_after=st, task_status=task_row(t1)["status"])

    # 8. Revoke API refuses without the key
    r = httpx.post(f"{API}/tasks/{t2}/revoke", json={}, timeout=10)
    check("revoke_needs_key", r.status_code == 401, status=r.status_code)

    OUT.write_text(json.dumps(results, indent=2))
    log(f"{sum(v['ok'] for v in results.values())}/{len(results)} passed -> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
