"""Teleop integration proof stack (laptop only; never touches maincloud or the Modal Volume).

  1. Throwaway SpacetimeDB on 127.0.0.1:$PORT (TDM-28 LocalStack; data under $TMPDIR/teleop-int/).
  2. Local stand-in for the Modal media store (spikes/tdm-58-recording LocalMediaStore).
  3. Orchestrator (orch_subset.py): dispatcher + TDM-25 reviewer (tests/integration/review_fixtures.yaml:
     fixtures allowed, Claude judgement off) + payouts + TDM-69 interventions; all HTTP routers.
  4. The real robot client: rdf-robot-client --sim --cameras synthetic, RDF_STREAM=none (LAN stream +
     /ws/control on 127.0.0.1), uploads through the orchestrator.
  5. A ticket on that robot with an approved clean_teleop data plan (1 episode): the dispatcher posts
     the marketplace task. run_settings.hyperparams_json.interventions_enabled = true, so a "Failing"
     flag on a sim rollout routes a recovery_takeover task + pause_for_takeover (TDM-69).
  6. Prints the web command. Stays up until Ctrl-C; `stack.py rollout` / `stack.py flag` drive session 2.

    cd services/orchestrator && uv run python ../../spikes/teleop-integration/stack.py up --port 3061
    uv run python ../../spikes/teleop-integration/stack.py rollout   # sim policy rollout on the robot
    uv run python ../../spikes/teleop-integration/stack.py flag      # operator "Failing" -> takeover task
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
HERE = Path(__file__).resolve().parent
os.environ.setdefault("TDM28_ROOT", str(Path(tempfile.gettempdir()) / "teleop-int"))
sys.path.insert(0, str(REPO / "tests" / "integration"))
sys.path.insert(0, str(REPO / "spikes" / "tdm-58-recording"))

from harness import REVIEW_CONFIG, LocalStack, wait_for  # noqa: E402

INFO = Path(os.environ["TDM28_ROOT"]) / "stack.json"
ROBOT_NAME = "test.teleop-int-sim"
OPERATOR_KEY = "teleop-int-throwaway-operator-key"  # local proof only; never a real key


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def up(a: argparse.Namespace) -> int:
    from e2e_stack import SIGNING_KEY, LocalMediaStore

    orch_port, stream_port = a.orch_port or free_port(), free_port()
    st = LocalStack(a.port, a.db)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    secret = "teleop-int-local-stream-secret-" + os.urandom(8).hex()
    local = {"SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db}
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        media = LocalMediaStore(st.root / "media-store")
        env = {**st.env("orchestrator"), "RDF_REVIEW_CONFIG": str(REVIEW_CONFIG), "RDF_MEDIA_BACKEND": "modal",
               "RDF_MEDIA_STORE_URL": media.url, "RDF_MEDIA_SIGNING_KEY": SIGNING_KEY,
               "RDF_STREAM_TOKEN_SECRET": secret, "INTERVENTION_CLAIM_TIMEOUT_S": "600",
               "INTERVENTION_TAKEOVER_MAX_S": "900", "INTERVENTION_COLLECT": "0",
               "RDF_OPERATOR_KEY": OPERATOR_KEY}
        st._spawn("orchestrator", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "orchestrator"),
                                   "python", str(HERE / "orch_subset.py"), "--port", str(orch_port)], env=env)
        wait_for(lambda: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{orch_port}/healthz", timeout=1).read())
                 ["subscription"]["state"] == "live", 120, "orchestrator live")
        print(f"[teleop-int] orchestrator on :{orch_port}", flush=True)

        st._spawn("robot", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"),
                            "rdf-robot-client", "--sim", "--cameras", "synthetic", "--name", ROBOT_NAME,
                            "--host", st.host, "--db", st.db, "--token-file", str(st.tokens / "robot_client.token")],
                  env={**local, "RDF_STREAM": "none", "RDF_STREAM_PORT": str(stream_port),
                       "RDF_STREAM_TOKEN_SECRET": secret, "ORCHESTRATOR_URL": f"http://127.0.0.1:{orch_port}",
                       "RDF_EPISODES_DIR": str(st.root / "episodes")})
        orch = st.client("orchestrator")
        robot = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM robot WHERE name = '{ROBOT_NAME}'")
                                       if r["status"] == "online"
                                       and json.loads(r["camera_config_json"] or "{}").get("stream")), None),
                         120, "sim robot online with a stream url")
        robot_id = int(robot["id"])
        print(f"[teleop-int] robot {robot_id} online: {json.loads(robot['camera_config_json'])['stream']}", flush=True)

        orch.call_reducer("create_ticket", robot_id, "[SIM] teleop integration: put the cube in the bowl", 0.8,
                          5000, json.dumps({"target_episodes": 1}))
        ticket_id = int(st.newest("ticket")["id"])
        for status in ("baseline", "diagnose", "plan_data", "dispatch"):  # no flywheel here: walk it by hand
            orch.call_reducer("set_ticket_status", ticket_id, status, "[FIXTURE] teleop integration proof")
        orch.call_reducer("update_run_settings", ticket_id, json.dumps({"hyperparams": {"interventions_enabled": True}}))
        orch.call_reducer("record_data_plan", ticket_id, "clean_teleop", 1,
                          "[FIXTURE] teleop integration proof: one clean demo", "{}")
        plan = st.newest("data_plan", f"ticket_id = {ticket_id}")
        orch.call_reducer("approve_spend", ticket_id, int(plan["id"]), 0, "human:teleop-int-proof")
        task = wait_for(lambda: st.newest("task", f"ticket_id = {ticket_id}"), 30, "dispatcher posts the clean_teleop task")
        info = {"spacetime": st.host, "db": st.db, "ticket_id": ticket_id, "robot_id": robot_id,
                "data_plan_id": int(plan["id"]), "clean_task_id": int(task["id"]),
                "orchestrator": f"http://127.0.0.1:{orch_port}", "stream_port": stream_port,
                "logs": str(st.logs), "root": str(st.root)}
        INFO.write_text(json.dumps(info, indent=2))
        print("[teleop-int] STACK READY " + json.dumps(info), flush=True)
        print(f"[teleop-int] web: cd apps/web && NEXT_DIST_DIR=.next-teleop-int NEXT_PUBLIC_SPACETIME_HOST={st.host} "
              f"NEXT_PUBLIC_SPACETIME_DB_NAME={st.db} RDF_ORCHESTRATOR_URL=http://127.0.0.1:{orch_port} "
              f"NEXT_PUBLIC_ORCHESTRATOR_URL=http://127.0.0.1:{orch_port} pnpm exec next dev --port <free>", flush=True)
        while not stop.is_set():
            dead = [n for n, p in st.procs.items() if p.poll() is not None]
            if dead:
                print(f"[teleop-int] process exited: {dead}; logs {st.logs}", flush=True)
                break
            stop.wait(1.0)
    finally:
        st.stop()
    return 0


def _client(info: dict):
    from rdf_common.spacetime_client import SpacetimeClient

    tok = (Path(info["root"]) / "tokens" / "orchestrator.token").read_text().strip()
    return SpacetimeClient(info["spacetime"], info["db"], tok)


def rollout(a: argparse.Namespace) -> int:
    """A sim policy rollout (run_baseline -> the --sim demo motion) long enough for a takeover."""
    info = json.loads(INFO.read_text())
    c = _client(info)
    c.call_reducer("issue_robot_command", info["robot_id"], "run_baseline",
                   json.dumps({"sim_seconds": a.seconds, "note": "[SIM] teleop integration rollout"}), info["ticket_id"])
    print(json.dumps(max(c.sql(f"SELECT * FROM robot_command WHERE robot_id = {info['robot_id']}"),
                         key=lambda r: int(r["id"])), default=str))
    return 0


def flag(a: argparse.Namespace) -> int:
    """The robot page's "Failing" button: POST /robots/{id}/interventions/flag (TDM-69)."""
    info = json.loads(INFO.read_text())
    req = urllib.request.Request(f"{info['orchestrator']}/robots/{info['robot_id']}/interventions/flag",
                                 data=json.dumps({"note": a.note, "by": "human:teleop-int-proof"}).encode(),
                                 headers={"content-type": "application/json", "x-rdf-operator-key": OPERATOR_KEY},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        print(r.read().decode())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("up")
    u.add_argument("--port", type=int, default=3061)
    u.add_argument("--db", default="teleopint")
    u.add_argument("--orch-port", type=int, default=0, help="fixed orchestrator port (the web server points at it)")
    r = sub.add_parser("rollout")
    r.add_argument("--seconds", type=float, default=900.0)
    f = sub.add_parser("flag")
    f.add_argument("--note", default="[SIM] policy is stuck above the bowl")
    a = ap.parse_args()
    return {"up": up, "rollout": rollout, "flag": flag}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
