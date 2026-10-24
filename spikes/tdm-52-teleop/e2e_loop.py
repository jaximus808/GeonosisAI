"""TDM-52 end-to-end on the laptop: the REAL robot-client command loop (--sim, synthetic cameras) on a
throwaway local SpacetimeDB, with the stream + /ws/control that main.py starts.

  1. boot spacetime on :3052 (db tdm52loop, data under $TMPDIR/tdm-52/), publish the working-tree module
  2. rdf-robot-client --sim --cameras synthetic   (RDF_STREAM=none, RDF_STREAM_PORT=8152, RDF_TELEOP_ALLOW_OPS=1)
  3. robot.camera_config_json["stream"] carries url + control_url
  4. issue start_teleop -> probe /ws/control with an ops token (3 cm square) -> stop_teleop
  5. the command's result_json reports the streamed steps; the arm moved

    cd services/robot-client && uv run python ../../spikes/tdm-52-teleop/e2e_loop.py

Never touches maincloud. (A task-lease-holder token is covered by tests/test_tdm52_teleop.py with the
TDM-57 guard; here the ops token needs RDF_TELEOP_ALLOW_OPS=1.)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack, wait_for  # noqa: E402

from rdf_common.stream_token import load_secret, mint  # noqa: E402
from rdf_robot_client.teleop import probe  # noqa: E402

PORT, DB, NAME, SPORT = 3052, "tdm52loop", "test.tdm52-sim", 8152


def main() -> int:
    root = Path(tempfile.gettempdir()) / "tdm-52"
    st = LocalStack(port=PORT, db=DB, root=root)
    proc = None
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        tok_file = st.make_identity("robot_client")
        orch = st.client("orchestrator")
        env = {**os.environ, "RDF_STREAM": "none", "RDF_STREAM_PORT": str(SPORT), "RDF_TELEOP_ALLOW_OPS": "1",
               "RDF_EPISODES_DIR": str(root / "episodes")}
        log = open(root / "robot-client.log", "w")
        proc = subprocess.Popen(["uv", "run", "rdf-robot-client", "--sim", "--cameras", "synthetic", "--name", NAME,
                                 "--host", st.host, "--db", DB, "--token-file", str(tok_file)],
                                cwd=REPO / "services/robot-client", env=env, stdout=log, stderr=subprocess.STDOUT)
        row = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM robot WHERE name = '{NAME}'")
                                     if r["status"] == "online"), None), 60, "robot online")
        cc = json.loads(row["camera_config_json"])
        stream = cc["stream"]
        print("camera_config.stream:", json.dumps(stream))
        assert stream["control_url"].endswith("/ws/control"), stream
        rid = int(row["id"])
        orch.call_reducer("issue_robot_command", rid, "start_teleop", json.dumps({"kind": "clean_teleop"}), 0)
        wait_for(lambda: any(r["type"] == "start_teleop" and r["status"] == "acked"
                             for r in orch.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid}")), 20, "teleop acked")
        token, _ = mint(load_secret(), robot_id=rid, robot_name=NAME, sub="ops:tdm52-e2e", ttl_s=120)
        url = stream["control_url"].replace("0.0.0.0", "127.0.0.1")
        res = asyncio.run(probe(url, token, pattern="square", size=0.03, seconds=6.0))
        print("probe:", json.dumps(res["last_state"], default=str))
        assert res["cmds"] > 100 and res["acks"] >= res["cmds"] - 2, res
        assert res["last_state"]["teleop"]["steps"] > 100, res
        orch.call_reducer("issue_robot_command", rid, "stop_teleop", "{}", 0)
        done = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid}")
                                      if r["type"] == "start_teleop" and r["status"] == "done"), None),
                        20, "start_teleop completed")
        result = json.loads(done["result_json"])
        print("start_teleop result:", json.dumps({k: result.get(k) for k in ("teleop", "steps", "seconds", "state")}))
        assert result["steps"] > 100
        print("TDM-52 e2e OK")
        return 0
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
        st.stop()


if __name__ == "__main__":
    sys.exit(main())
