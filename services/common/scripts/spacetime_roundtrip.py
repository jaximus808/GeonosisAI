"""TDM-16 proof: call a reducer and print the insert event from our own subscription (< 1 s).

    cd services/common
    uv run python scripts/spacetime_roundtrip.py            # event_log row, type test.tdm16.roundtrip
    uv run python scripts/spacetime_roundtrip.py --ticket   # create_ticket (needs a robot row; writes a test ticket)
    SPACETIME_HOST=http://127.0.0.1:3917 SPACETIME_DB_NAME=<local db> uv run python scripts/spacetime_roundtrip.py --ticket

Uses the shared helper exactly as a service would: one keep-alive SpacetimeClient for reducer
calls and a threaded Subscription (sync mode, as on the Jetson / in Modal jobs). Reads
SPACETIME_HOST / SPACETIME_DB_NAME / SPACETIME_TOKEN from the env or the repo .env (never printed).
Exits 1 if the event does not arrive within --budget seconds (default 1.0).
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import time
import uuid

from rdf_common.spacetime_client import SpacetimeClient, load_dotenv, subscribe, ts_to_datetime
from rdf_common.spacetime_tables import EventLog, Reducers, Ticket

TYPE = "test.tdm16.roundtrip"


def _defaults(schema, reducer: str, given: dict) -> dict:
    """Fill args the script does not know with zero values of their type (smoke test only)."""
    out = {}
    for name, ty in schema.reducers[reducer]:
        if name in given:
            out[name] = given[name]
            continue
        t = schema.resolve(ty)
        k = next(iter(t))
        out[name] = {"String": "", "Bool": False, "F64": 0.0, "F32": 0.0, "Array": []}.get(k, 0 if k[0] in "IU" else None)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticket", action="store_true", help="create a test ticket via create_ticket instead of an event_log row")
    ap.add_argument("--budget", type=float, default=1.0)
    ap.add_argument("--robot-id", type=int, help="robot for --ticket (default: the first robot row)")
    a = ap.parse_args()
    load_dotenv()

    stdb = SpacetimeClient()
    schema = stdb.schema()  # also warms the keep-alive connection
    nonce = uuid.uuid4().hex[:8]
    if a.ticket:
        if "create_ticket" not in schema.reducers:
            print("create_ticket is not published yet (TDM-8 reducer tickets); rerun later. Reducers:",
                  ", ".join(sorted(schema.reducers)))
            return 2
        table, query = "ticket", "SELECT * FROM ticket"
        match = lambda row: nonce in row.task_description  # noqa: E731
    else:
        table, query = "event_log", f"SELECT * FROM event_log WHERE type = '{TYPE}'"
        match = lambda row: json.loads(row.payload_json).get("nonce") == nonce  # noqa: E731

    inbox: queue.Queue = queue.Queue()
    sub = subscribe([query], typed=True, on_insert=lambda t, row, meta: inbox.put((t, row, meta)),
                    on_error=lambda e: print(f"  (subscription: {type(e).__name__}: {e})"))
    if not sub.wait_ready(15):
        print("subscription did not apply within 15 s")
        return 1
    print(f"subscribed as {sub.identity[:18]}...: {len(sub.rows(table))} existing rows in {table}")

    t0 = time.perf_counter()
    if a.ticket:
        robot_id = a.robot_id or min((r["id"] for r in stdb.sql("SELECT * FROM robot")), default=None)
        if robot_id is None:
            print("no robot row to attach the test ticket to; pass --robot-id")
            return 2
        args = _defaults(schema, "create_ticket", {
            "robot_id": robot_id, "task_description": f"[test.tdm16] roundtrip {nonce}",
            "target_success": 0.8, "budget_cents": 0, "settings_overrides_json": "{}",
        })
        stdb.call_reducer("create_ticket", **args)
    else:
        Reducers(stdb).append_event(actor="tdm16-roundtrip", type=TYPE, payload_json=json.dumps({"nonce": nonce}))
    call_ms = (time.perf_counter() - t0) * 1000

    deadline = t0 + max(a.budget, 5.0)
    while time.perf_counter() < deadline:
        try:
            t, row, meta = inbox.get(timeout=max(0.01, deadline - time.perf_counter()))
        except queue.Empty:
            break
        if t == table and match(row):
            lag = time.perf_counter() - t0
            assert isinstance(row, (EventLog, Ticket))
            ts = row.ts if isinstance(row, EventLog) else row.created_at
            print(f"insert {table}: {row}")
            print(f"server ts {ts_to_datetime(ts).isoformat()}  reducer call {call_ms:.0f} ms, "
                  f"event received {lag * 1000:.0f} ms after the call")
            sub.stop()
            stdb.close()
            ok = lag <= a.budget
            print("OK" if ok else f"TOO SLOW (budget {a.budget:.1f} s)")
            return 0 if ok else 1
    print("no insert event received")
    sub.stop()
    return 1


if __name__ == "__main__":
    sys.exit(main())
