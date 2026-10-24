"""TDM-96 proof: the REAL ``rdf-robot-client --sim --cameras synthetic`` against a throwaway local
SpacetimeDB, its ``/ws/state`` read with a VIEW token, measured, and recorded as the web fixture.

    cd services/orchestrator && uv run python ../../spikes/tdm-96-state/prove_state.py [--keep] [--seconds 6]

* SpacetimeDB on 127.0.0.1:3196 (db tdm96state), data under spikes/tdm-96-state/tmp/ (gitignored); never
  maincloud, never the user's demo ports (3028 / 8028 / 3737).
* Robot stream (+ /ws/state) on 127.0.0.1:8196 (RDF_STREAM=none, LAN).
* Issues ``run_baseline {sim_seconds}`` (the --sim scripted sine sweep: SIM motion, no policy) so the arm moves.
* Prints Hz + sample->receive latency (clock-offset corrected, min-RTT ping) and writes
  apps/web/src/lib/robotview/fixtures/sim_arm_motion.json (labelled fixture/sim).
* ``--keep`` leaves the stack up for the browser check and prints a view token + ws URL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
os.environ.setdefault("TDM28_ROOT", str(REPO / "spikes" / "tdm-96-state" / "tmp"))

from harness import LocalStack, wait_for  # noqa: E402

PORT, DB, STREAM_PORT = 3196, "tdm96state", 8196
FIXTURE = REPO / "apps" / "web" / "src" / "lib" / "robotview" / "fixtures" / "sim_arm_motion.json"


async def read_state(url: str, token: str, seconds: float) -> dict:
    import websockets

    states, pongs = [], []
    async with websockets.connect(url, max_size=1 << 20) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        hello = json.loads(await ws.recv())
        assert hello.get("t") == "hello", hello

        async def pinger():
            while True:
                await ws.send(json.dumps({"type": "ping", "c": time.time() * 1000}))
                await asyncio.sleep(0.5)

        pt = asyncio.create_task(pinger())
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                m = json.loads(await asyncio.wait_for(ws.recv(), 2))
                rx = time.time() * 1000
                if m.get("t") == "state":
                    states.append((rx, m))
                elif m.get("t") == "pong":
                    pongs.append((rx - m["c"], m["server_ms"] - (m["c"] + rx) / 2))
        finally:
            pt.cancel()
    off = min(pongs)[1] if pongs else 0.0
    lat = sorted(rx - (m["t_ms"] - off) for rx, m in states)
    gaps = [b[0] - a[0] for a, b in zip(states, states[1:])]
    return {"hello": hello, "n": len(states), "hz": round(len(states) / seconds, 1),
            "latency_ms_p50": round(lat[len(lat) // 2], 2), "latency_ms_p95": round(lat[int(len(lat) * 0.95)], 2),
            "gap_ms_max": round(max(gaps), 1) if gaps else None, "rtt_ms_min": round(min(pongs)[0], 2) if pongs else None,
            "states": [m for _, m in states]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--no-fixture", action="store_true")
    a = ap.parse_args()
    from rdf_common.stream_token import load_secret, mint

    st = LocalStack(PORT, DB)
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        st._spawn("sim-robot", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"),
                                "rdf-robot-client", "--sim", "--cameras", "synthetic", "--host", st.host, "--db", st.db,
                                "--token-file", str(st.tokens / "robot_client.token")],
                  env={"RDF_STREAM": "none", "RDF_STREAM_PORT": str(STREAM_PORT)})
        robot = st.robot("test.sim-yam-1", timeout=90)
        rid = int(robot["id"])
        info = json.loads(robot["camera_config_json"])["stream"]
        state_url = info["state_url"].replace(info["state_url"].split("//")[1].split(":")[0], "127.0.0.1")
        print(f"[tdm96] robot {rid} online; state_url {info['state_url']} (using {state_url})", file=sys.stderr)
        tok = mint(load_secret(), robot_id=rid, robot_name=robot["name"], sub="operator:view", scope="view",
                   ttl_s=3600 if a.keep else 120)[0]
        st.client("orchestrator").call_reducer("issue_robot_command", rid, "run_baseline",
                                               json.dumps({"sim_seconds": 3600 if a.keep else 60}), 0)
        wait_for(lambda: st.sql(f"SELECT * FROM robot WHERE id = {rid}")[0]["status"] == "busy", 30, "robot busy")
        time.sleep(1.5)  # past the homing move, into the sine sweep
        r = asyncio.run(read_state(state_url, tok, a.seconds))
        s = r.pop("states")
        print(json.dumps({k: v for k, v in r.items() if k != "hello"}, indent=2))
        print(json.dumps({"last": s[-1]}, indent=1)[:1500], file=sys.stderr)
        if not a.no_fixture:
            take = [m for m in s if m["control_source"] == "policy"][: int(4 * r["hz"])] or s[:120]
            fx = {"fixture": True, "sim": True,
                  "label": "FIXTURE / SIMULATED: recorded /ws/state of rdf-robot-client --sim (SimArm kinematic sim, "
                           "safety layer on), during the --sim scripted run_baseline sine sweep. No policy ran.",
                  "recorded_from": "spikes/tdm-96-state/prove_state.py (rdf-robot-client --sim --cameras synthetic, "
                                   "RDF_STREAM=none, view token)",
                  "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "hz": r["hz"], "frames": take}
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(json.dumps(fx, separators=(",", ":")) + "\n")
            print(f"[tdm96] wrote {FIXTURE} ({len(take)} frames)", file=sys.stderr)
        if a.keep:
            print(json.dumps({"state_url": state_url, "view_token": tok, "robot_id": rid,
                              "spacetime": st.host, "db": st.db}), flush=True)
            print("[tdm96] stack up; Ctrl-C to stop", file=sys.stderr)
            while True:
                time.sleep(1)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        st.stop()


if __name__ == "__main__":
    sys.exit(main())
