"""TDM-18 proof: event_log insert -> orchestrator handler log line, timed.

Start the orchestrator with its log captured, then ping:

    cd services/orchestrator
    uv run rdf-orchestrator --port 8018 2> /tmp/orch.log &
    uv run python scripts/tdm18_proof.py --log /tmp/orch.log --health http://127.0.0.1:8018/healthz -n 5

Each ping calls the append_event reducer (keep-alive HTTP) with type ``test.tdm18.ping`` and a
unique ``ticket_id`` in the payload, then waits for the orchestrator's ``event_log.insert`` line
carrying that ticket_id. Prints wall time from the reducer call to the log line.
``--wait-reconnect`` first waits until /healthz reports one more reconnect than at start (kill and
restart the SpacetimeDB server meanwhile), then pings.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid

import httpx
from rdf_common.settings import SpacetimeSettings, load_settings


def health(c: httpx.Client, url: str) -> dict:
    return c.get(url, timeout=3).json()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--health", default="http://127.0.0.1:8018/healthz")
    ap.add_argument("-n", type=int, default=3)
    ap.add_argument("--wait-reconnect", action="store_true")
    ap.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args()
    s = load_settings(SpacetimeSettings, "tdm18-proof")
    base = f"{s.spacetime_host.rstrip('/')}/v1/database/{s.spacetime_db_name}"
    hdr = {"Authorization": f"Bearer {s.spacetime_token.get_secret_value()}", "Content-Type": "application/json"}

    with httpx.Client() as c:
        h0 = health(c, a.health)
        print(f"healthz: state={h0['subscription']['state']} reconnects={h0['subscription']['reconnects']}")
        if a.wait_reconnect:
            want = h0["subscription"]["reconnects"] + 1
            print(f"waiting for reconnects >= {want} (restart the SpacetimeDB server now)...")
            seen_down = False
            while True:
                try:
                    h = health(c, a.health)
                except httpx.HTTPError:
                    time.sleep(0.5)
                    continue
                st = h["subscription"]["state"]
                if st != "live" and not seen_down:
                    seen_down = True
                    print(f"  orchestrator sees state={st}: {h['subscription']['last_error']}")
                if h["subscription"]["reconnects"] >= want and st == "live":
                    print(f"  re-subscribed: state=live reconnects={h['subscription']['reconnects']} (same orchestrator process, uptime {h['uptime_s']} s)")
                    break
                time.sleep(0.5)
        c.post(f"{base}/call/append_event", headers=hdr, content=json.dumps(["tdm18-proof", "test.tdm18.warmup", "{}"]))
        worst = 0.0
        with open(a.log) as f:
            f.seek(0, 2)
            for i in range(a.n):
                tid = f"test-tdm18-{uuid.uuid4().hex[:8]}"
                t0 = time.time()
                r = c.post(f"{base}/call/append_event", headers=hdr,
                           content=json.dumps(["tdm18-proof", "test.tdm18.ping", json.dumps({"ticket_id": tid, "i": i})]))
                r.raise_for_status()
                t_call = time.time()
                line = None
                while time.time() - t0 < a.timeout and line is None:
                    for raw in f.readlines():
                        try:
                            d = json.loads(raw)
                        except ValueError:
                            continue
                        if d.get("msg") == "event_log.insert" and d.get("ticket_id") == tid:
                            line = d
                    if line is None:
                        time.sleep(0.005)
                t1 = time.time()
                if line is None:
                    raise SystemExit(f"FAIL: no handler log line for {tid} within {a.timeout}s")
                total, after = (t1 - t0) * 1000, (t1 - t_call) * 1000
                worst = max(worst, total)
                print(f"  ping {i}: event_id={line['event_id']} ticket_id={tid} reducer_call={(t_call - t0) * 1000:.0f} ms, "
                      f"call->log line={total:.0f} ms (after call returned: {after:.0f} ms, handler lag_ms={line['lag_ms']})")
                print(f"    {json.dumps(line)}")
        h = health(c, a.health)
        print(f"healthz after: status={h['status']} state={h['subscription']['state']} reconnects={h['subscription']['reconnects']} "
              f"handler_errors={h['handler_errors']} last_event={h['last_event']}")
        print(f"{'OK' if worst < 1000 else 'SLOW'}: worst call->handler log line {worst:.0f} ms")


if __name__ == "__main__":
    main()
