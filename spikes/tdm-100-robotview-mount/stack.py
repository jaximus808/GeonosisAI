"""TDM-100: a throwaway local stack to see RobotView mounted on /robots/[id] and /tickets/[id].

    cd services/orchestrator && uv run python ../../spikes/tdm-100-robotview-mount/stack.py

  * SpacetimeDB on 127.0.0.1:$TDM100_PORT (default 3110), db ``tdm100mount``, data under $TMPDIR/tdm-100
    (wiped on every boot; never maincloud, never the demo stack on 3028 / 8028 / 3737).
  * ``rdf-robot-client --sim --cameras synthetic`` named ``sim-yam-tdm100`` (stream + /ws/state on
    127.0.0.1:$TDM100_STREAM_PORT, default 8210, throwaway RDF_STREAM_TOKEN_SECRET).
  * An offline live robot row ``yam-desk-tdm100`` (embodiment i2rt_yam).
  * Ticket A on the sim robot in ``baseline`` (the "Watch the robot" panel opens), kept moving by
    re-issuing the --sim demo ``run_baseline`` (a scripted sine sweep: no policy, no eval trials).
  * Ticket B on the sim robot left ``created`` (the panel starts collapsed).

Then run the web app against it (own port + dist dir) with the env this prints, and open the printed
URLs (the ops token rides in the fragment, so it never reaches a server). Ctrl-C stops everything.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
os.environ.setdefault("TDM28_ROOT", str(Path(os.environ.get("TMPDIR", "/tmp")) / "tdm-100"))

from harness import LocalStack, wait_for  # noqa: E402

PORT = int(os.environ.get("TDM100_PORT", "3110"))
STREAM_PORT = int(os.environ.get("TDM100_STREAM_PORT", "8210"))
WEB_PORT = int(os.environ.get("TDM100_WEB_PORT", "3790"))
DB = "tdm100mount"
SIM_NAME = "sim-yam-tdm100"
LIVE_NAME = "yam-desk-tdm100"


def main() -> None:
    st = LocalStack(port=PORT, db=DB)
    st.start_server()
    st.publish()
    st.make_identity("orchestrator")
    st.make_identity("robot_client")
    secret = secrets.token_urlsafe(32)  # throwaway, this stack only
    (st.tokens / "stream_secret").write_text(secret)
    os.chmod(st.tokens / "stream_secret", 0o600)
    env = {"RDF_STREAM": "none", "RDF_STREAM_PORT": str(STREAM_PORT), "RDF_STREAM_TOKEN_SECRET": secret}
    st._spawn("sim-robot", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"),
                            "rdf-robot-client", "--sim", "--cameras", "synthetic", "--name", SIM_NAME,
                            "--host", st.host, "--db", DB, "--token-file", str(st.tokens / "robot_client.token")],
              env=env)
    sim = st.robot(SIM_NAME, timeout=90)
    sim_id = int(sim["id"])
    c = st.client("orchestrator")
    c.call_reducer("register_robot", LIVE_NAME, "i2rt_yam", "yam-sdk-0.3",
                   json.dumps({"cameras": "config", "stream": {"url": "wss://offline.invalid/ws/cameras",
                                                                "cams": ["top", "wrist"]}}))
    live = c.sql(f"SELECT * FROM robot WHERE name = '{LIVE_NAME}'")[0]
    c.call_reducer("robot_heartbeat", int(live["id"]), "offline")
    wait_for(lambda: "stream" in json.loads(c.sql(f"SELECT * FROM robot WHERE name = '{SIM_NAME}'")[0]
                                           ["camera_config_json"]), 60, "sim robot stream announcement")

    c.call_reducer("create_ticket", sim_id, "test.tdm100 pick the cube and place it on the plate [SIM]", 0.8, 5000, "{}")
    c.call_reducer("create_ticket", sim_id, "test.tdm100 idle ticket (panel collapsed) [SIM]", 0.8, 5000, "{}")
    tickets = sorted(c.sql("SELECT * FROM ticket"), key=lambda r: int(r["id"]))
    ta, tb = int(tickets[0]["id"]), int(tickets[1]["id"])
    c.call_reducer("set_ticket_status", ta, "baseline", "test.tdm100: show the watch panel")

    tok = subprocess.run(["uv", "run", "--quiet", "--project", str(REPO / "services" / "orchestrator"), "python", "-m",
                          "rdf_common.stream_token", "mint", "--robot-id", str(sim_id), "--robot-name", SIM_NAME,
                          "--scope", "view", "--ttl", "21600", "--sub", "ops:tdm100"],
                         env={**os.environ, "RDF_STREAM_TOKEN_SECRET": secret},
                         capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    (st.root / "ops_token").write_text(tok)
    base = f"http://127.0.0.1:{WEB_PORT}"
    print(f"web env: NEXT_PUBLIC_SPACETIME_HOST={st.host} NEXT_PUBLIC_SPACETIME_DB_NAME={DB} "
          f"NEXT_DIST_DIR=.next-tdm100 (next dev --port {WEB_PORT})")
    print(f"sim robot {sim_id}, live robot {live['id']}, ticket A {ta} (baseline), ticket B {tb} (created)")
    print(f"ops token written to {st.root / 'ops_token'} (append #stream_token=<it> to the URLs)")
    print(f"  {base}/robots/{sim_id}\n  {base}/tickets/{ta}\n  {base}/tickets/{tb}\n  {base}/robots/{live['id']}",
          flush=True)

    def open_cmd() -> bool:
        rows = c.sql(f"SELECT * FROM robot_command WHERE robot_id = {sim_id}")
        return any(r["status"] in ("issued", "acked") and r["type"] == "run_baseline" for r in rows)

    try:
        while True:
            if not open_cmd():
                c.call_reducer("issue_robot_command", sim_id, "run_baseline", json.dumps({"sim_seconds": 120}), ta)
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        st.stop()


if __name__ == "__main__":
    main()
