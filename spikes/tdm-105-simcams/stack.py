"""TDM-105 proof stack: a MuJoCo [SIM] YAM with NO cameras, driven from /teleop/<task> (laptop only).

The sim robot runs ``--adapter mujoco --cameras off``: no video stream at all, only /ws/control and
/ws/state. The teleop page must still be drivable, showing the browser-rendered top / side / wrist
views (components/robotview/SimCameras.tsx) instead of "No video / 503".

  1. Throwaway SpacetimeDB on 127.0.0.1:$PORT (TDM-28 LocalStack, data under $TMPDIR/tdm-105/).
  2. Orchestrator subset (spikes/teleop-integration/orch_subset.py): dispatcher + reviewer + payouts, all
     HTTP routers (/stream/token). No flywheel, so no Claude calls and no baseline eval on the robot.
  3. rdf-robot-client --adapter mujoco --cameras off (RDF_STREAM=none: LAN stream server on 127.0.0.1).
  4. A ticket walked by hand to dispatch with an approved 1-episode clean_teleop plan; the dispatcher
     posts the task. A test contributor identity claims it and keeps the lease alive; its SpacetimeDB
     token is written (0600) to $TDM28_ROOT/browser.token for the browser tab, never printed.

    cd services/orchestrator && uv run python ../../spikes/tdm-105-simcams/stack.py --port 3115 --orch-port 8115
    then the web: the command it prints (own NEXT_DIST_DIR), and open /teleop/<task id>.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("TDM28_ROOT", str(Path(tempfile.gettempdir()) / "tdm-105"))
sys.path.insert(0, str(REPO / "tests" / "integration"))
sys.path.insert(0, str(REPO / "spikes" / "tdm-58-recording"))

from harness import REVIEW_CONFIG, LocalStack, wait_for  # noqa: E402

ROBOT_NAME = "test.tdm105-mujoco-nocam"
OPERATOR_KEY = "tdm105-throwaway-operator-key"  # local proof only; never a real key


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3115)
    ap.add_argument("--db", default="tdm105simcams")
    ap.add_argument("--orch-port", type=int, default=8115)
    a = ap.parse_args()

    from e2e_stack import SIGNING_KEY, LocalMediaStore

    stream_port = free_port()
    st = LocalStack(a.port, a.db)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    secret = "tdm105-local-stream-secret-" + os.urandom(8).hex()
    local = {"SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db}
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        st.make_identity("web")
        media = LocalMediaStore(st.root / "media-store")
        env = {**st.env("orchestrator"), "RDF_REVIEW_CONFIG": str(REVIEW_CONFIG), "RDF_MEDIA_BACKEND": "modal",
               "RDF_MEDIA_STORE_URL": media.url, "RDF_MEDIA_SIGNING_KEY": SIGNING_KEY,
               "RDF_STREAM_TOKEN_SECRET": secret, "INTERVENTION_COLLECT": "0", "RDF_OPERATOR_KEY": OPERATOR_KEY}
        st._spawn("orchestrator", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "orchestrator"),
                                   "python", str(REPO / "spikes" / "teleop-integration" / "orch_subset.py"),
                                   "--port", str(a.orch_port)], env=env)
        wait_for(lambda: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{a.orch_port}/healthz", timeout=1).read())
                 ["subscription"]["state"] == "live", 120, "orchestrator live")

        st._spawn("robot", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "--extra", "sim",
                            "rdf-robot-client", "--adapter", "mujoco", "--cameras", "off", "--name", ROBOT_NAME,
                            "--host", st.host, "--db", st.db, "--token-file", str(st.tokens / "robot_client.token")],
                  env={**local, "RDF_STREAM": "none", "RDF_STREAM_PORT": str(stream_port),
                       "RDF_STREAM_TOKEN_SECRET": secret, "ORCHESTRATOR_URL": f"http://127.0.0.1:{a.orch_port}",
                       "RDF_EPISODES_DIR": str(st.root / "episodes"),
                       "MUJOCO_GL": os.environ.get("MUJOCO_GL", "cgl" if sys.platform == "darwin" else "egl")})
        orch = st.client("orchestrator")
        robot = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM robot WHERE name = '{ROBOT_NAME}'")
                                       if r["status"] == "online"
                                       and json.loads(r["camera_config_json"] or "{}").get("stream")), None),
                         180, f"sim robot online with its control/state server (log {st.logs / 'robot.log'})")
        robot_id = int(robot["id"])
        print(f"[tdm105] robot {robot_id} online: {json.loads(robot['camera_config_json']).get('stream')}", flush=True)

        orch.call_reducer("create_ticket", robot_id, "[SIM] tdm105: pick up the cube and place it in the empty plate",
                          0.8, 5000, json.dumps({"target_episodes": 1}))
        ticket_id = int(st.newest("ticket")["id"])
        for status in ("baseline", "diagnose", "plan_data", "dispatch"):  # no flywheel: walk it by hand
            orch.call_reducer("set_ticket_status", ticket_id, status, "[FIXTURE] tdm105 sim views proof")
        orch.call_reducer("record_data_plan", ticket_id, "clean_teleop", 1, "[FIXTURE] tdm105: one clean demo", "{}")
        plan = st.newest("data_plan", f"ticket_id = {ticket_id}")
        orch.call_reducer("approve_spend", ticket_id, int(plan["id"]), 0, "human:tdm105-proof")
        task = wait_for(lambda: st.newest("task", f"ticket_id = {ticket_id}"), 30, "dispatcher posts the clean_teleop task")
        task_id = int(task["id"])

        from rdf_common.spacetime_client import SpacetimeClient, create_identity

        _ident, ctoken = create_identity(st.host)
        (st.root / "browser.token").write_text(ctoken)
        os.chmod(st.root / "browser.token", 0o600)
        contrib = SpacetimeClient(st.host, st.db, ctoken)
        contrib.call_reducer("register_contributor", "test.tdm105 operator", "", ["web"], "contributor", ticket_id)
        cid = int(max((r for r in st.sql("SELECT * FROM contributor") if r["name"] == "test.tdm105 operator"),
                      key=lambda r: int(r["id"]))["id"])
        contrib.call_reducer("claim_task", task_id, cid)

        info = {"spacetime": st.host, "db": st.db, "ticket_id": ticket_id, "robot_id": robot_id, "task_id": task_id,
                "contributor_id": cid, "orchestrator": f"http://127.0.0.1:{a.orch_port}", "stream_port": stream_port,
                "browser_token_file": str(st.root / "browser.token"), "logs": str(st.logs)}
        (st.root / "stack.json").write_text(json.dumps(info, indent=2))
        print("[tdm105] STACK READY " + json.dumps(info), flush=True)
        print(f"[tdm105] web: cd apps/web && NEXT_DIST_DIR=.next-tdm105 NEXT_PUBLIC_SPACETIME_HOST={st.host} "
              f"NEXT_PUBLIC_SPACETIME_DB_NAME={st.db} RDF_ORCHESTRATOR_URL=http://127.0.0.1:{a.orch_port} "
              f"NEXT_PUBLIC_ORCHESTRATOR_URL=http://127.0.0.1:{a.orch_port} pnpm exec next dev --port <free>", flush=True)

        last_hb = 0.0
        while not stop.is_set():
            if time.monotonic() - last_hb > 15:
                try:
                    contrib.call_reducer("heartbeat_claim", task_id)
                except Exception as e:  # noqa: BLE001
                    print(f"[tdm105] heartbeat failed: {e}", flush=True)
                last_hb = time.monotonic()
            dead = [n for n, p in st.procs.items() if p.poll() is not None]
            if dead:
                print(f"[tdm105] process exited: {dead}; logs {st.logs}", flush=True)
                break
            stop.wait(1.0)
    finally:
        st.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
