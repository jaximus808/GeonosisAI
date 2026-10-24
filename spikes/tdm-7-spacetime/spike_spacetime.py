# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=13"]
# ///
"""TDM-7 / S-02 proof: Python calls a reducer and receives the live subscription event.

    uv run spikes/tdm-7-spacetime/spike_spacetime.py            # HTTP call + WS call + reconnect check
    uv run spikes/tdm-7-spacetime/spike_spacetime.py --thread   # same, but sync code via start_thread()

Writes rows to event_log with type 'spike.tdm7' (append-only; harmless test rows).
Reads SPACETIME_HOST / SPACETIME_DB_NAME / SPACETIME_TOKEN from the repo .env (never printed).
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import queue
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from stdb_ws import StdbSubscriber, call_reducer_http, sql, ts_micros  # noqa: E402

TYPE = "spike.tdm7"
QUERY = f"SELECT * FROM event_log WHERE type = '{TYPE}'"


def load_env() -> None:
    env = pathlib.Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def show(tag: str, row: dict, meta: dict, sent_at: float) -> None:
    lag_ms = (time.perf_counter() - sent_at) * 1000
    print(f"  [{tag}] insert event_log id={row['id']} actor={row['actor']} type={row['type']} "
          f"payload={row['payload_json']} reducer={meta['reducer']} ts={ts_micros(row['ts'])} "
          f"-> {lag_ms:.0f} ms after call")


async def main_async() -> int:
    inbox: asyncio.Queue = asyncio.Queue()
    sub = StdbSubscriber(
        [QUERY],
        on_connect=lambda ident, _tok: print(f"connected as identity {ident[:18]}..."),
        on_applied=lambda t: print(f"subscription applied: {len(t.get('event_log', []))} existing '{TYPE}' rows"),
        on_insert=lambda table, row, meta: inbox.put_nowait((table, row, meta)),
        on_error=lambda e: print(f"  (ws error/drop: {type(e).__name__}: {e})"),
    )
    task = asyncio.create_task(sub.run())
    await asyncio.wait_for(sub.connected.wait(), 15)

    async def expect(nonce: str, tag: str, sent_at: float) -> None:
        while True:
            table, row, meta = await asyncio.wait_for(inbox.get(), 15)
            if json.loads(row["payload_json"]).get("nonce") == nonce:
                show(tag, row, meta, sent_at)
                return

    # 1) reducer via HTTP, event via WS subscription
    n1 = uuid.uuid4().hex[:8]
    t = time.perf_counter()
    await asyncio.to_thread(call_reducer_http, "append_event", ["tdm7-spike", TYPE, json.dumps({"via": "http", "nonce": n1})])
    await expect(n1, "http-call", t)

    # 2) reducer over the same WebSocket
    n2 = uuid.uuid4().hex[:8]
    t = time.perf_counter()
    await sub.call_reducer("append_event", ["tdm7-spike", TYPE, json.dumps({"via": "ws", "nonce": n2})])
    await expect(n2, "ws-call", t)

    # 3) drop the socket; the client must reconnect, resubscribe and still see new rows
    print("forcing a disconnect...")
    await sub._ws.close()
    await asyncio.sleep(0.2)
    await asyncio.wait_for(sub.connected.wait(), 30)
    n3 = uuid.uuid4().hex[:8]
    t = time.perf_counter()
    await asyncio.to_thread(call_reducer_http, "append_event", ["tdm7-spike", TYPE, json.dumps({"via": "http-after-reconnect", "nonce": n3})])
    await expect(n3, "after-reconnect", t)

    # 4) SQL read-back (the polling fallback path)
    rows = await asyncio.to_thread(sql, f"{QUERY}")
    print(f"SQL read-back: {sum(1 for r in rows if json.loads(r['payload_json']).get('nonce') in (n1, n2, n3))}/3 spike rows present")

    sub.stop()
    await asyncio.wait_for(task, 10)
    print("OK: Python reducer call + live subscription proven against maincloud")
    return 0


def main_thread() -> int:
    """Sync usage, as the robot client / Modal jobs would do it."""
    inbox: queue.Queue = queue.Queue()
    ready = queue.Queue()
    sub = StdbSubscriber(
        [QUERY],
        on_applied=lambda t: ready.put(True),
        on_insert=lambda table, row, meta: inbox.put((row, meta)),
    )
    sub.start_thread()
    ready.get(timeout=15)
    print("subscriber thread up")
    nonce = uuid.uuid4().hex[:8]
    t = time.perf_counter()
    call_reducer_http("append_event", ["tdm7-spike", TYPE, json.dumps({"via": "http-sync", "nonce": nonce})])
    deadline = time.time() + 15
    while time.time() < deadline:
        row, meta = inbox.get(timeout=15)
        if json.loads(row["payload_json"]).get("nonce") == nonce:
            show("sync-thread", row, meta, t)
            sub.stop()
            print("OK: sync/threaded subscriber works")
            return 0
    return 1


if __name__ == "__main__":
    load_env()
    sys.exit(main_thread() if "--thread" in sys.argv else asyncio.run(main_async()))
