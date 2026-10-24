"""TDM-98: a throwaway local stack for the <RobotView> prototype (/dev/robotview).

    cd services/orchestrator && uv run python ../../spikes/tdm-98-robotview/stack.py up

  * SpacetimeDB on 127.0.0.1:$TDM98_PORT (default 3098), db ``tdm98view``, data under $TMPDIR/tdm-98
    (wiped on every boot; never maincloud, never the demo stack on 3028 / 8028 / 3737).
  * ``rdf-robot-client --sim --cameras synthetic`` named ``sim-yam-tdm98`` (stream server on
    127.0.0.1:$TDM98_STREAM_PORT, default 8198, LAN mode, a throwaway RDF_STREAM_TOKEN_SECRET).
  * A second robot row ``yam-desk-tdm98`` (embodiment i2rt_yam, so the registry calls it live) that is
    offline: registered by the orchestrator identity, one ``offline`` heartbeat, cameras announced but
    nothing serving them.

Prints the NEXT_PUBLIC_* env for the web app and an ops stream token for the sim robot's cameras.
Ctrl-C stops everything.
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
os.environ.setdefault("TDM28_ROOT", str(Path(os.environ.get("TMPDIR", "/tmp")) / "tdm-98"))

from harness import LocalStack, wait_for  # noqa: E402

PORT = int(os.environ.get("TDM98_PORT", "3098"))
STREAM_PORT = int(os.environ.get("TDM98_STREAM_PORT", "8198"))
DB = "tdm98view"
SIM_NAME = "sim-yam-tdm98"
LIVE_NAME = "yam-desk-tdm98"


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
    c = st.client("orchestrator")
    c.call_reducer("register_robot", LIVE_NAME, "i2rt_yam", "yam-sdk-0.3",
                   json.dumps({"cameras": "config", "stream": {"url": "wss://offline.invalid/ws/cameras",
                                                                "cams": ["top", "wrist"]}}))
    live = c.sql(f"SELECT * FROM robot WHERE name = '{LIVE_NAME}'")[0]
    c.call_reducer("robot_heartbeat", int(live["id"]), "offline")
    wait_for(lambda: "stream" in json.loads(c.sql(f"SELECT * FROM robot WHERE name = '{SIM_NAME}'")[0]
                                           ["camera_config_json"]), 60, "sim robot stream announcement")
    tok = subprocess.run(["uv", "run", "--quiet", "--project", str(REPO / "services" / "orchestrator"), "python", "-m",
                          "rdf_common.stream_token", "mint", "--robot-id", str(sim["id"]), "--robot-name", SIM_NAME, "--scope", "view", "--ttl", "21600",
                          "--sub", "ops:tdm98"], env={**os.environ, "RDF_STREAM_TOKEN_SECRET": secret},
                         capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    print(f"NEXT_PUBLIC_SPACETIME_HOST={st.host} NEXT_PUBLIC_SPACETIME_DB_NAME={DB}")
    print(f"sim robot id {sim['id']}, live robot id {live['id']}")
    print(f"ops token (sim cameras): #stream_token={tok}", flush=True)
    (st.root / "ops_token").write_text(tok)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        st.stop()


if __name__ == "__main__":
    main()
