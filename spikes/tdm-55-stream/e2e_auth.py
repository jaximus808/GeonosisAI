"""TDM-55 end-to-end auth check on a throwaway LOCAL SpacetimeDB (never maincloud).

claimant identity -> claim_task -> POST /stream/token (real router, real identity verify, real SQL)
-> robot-client stream server accepts the token and sends frames; a stranger gets 403; after
release_task the claimant gets 403 too.

    cd services/orchestrator && uv run python ../../spikes/tdm-55-stream/e2e_auth.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
from harness import LocalStack  # noqa: E402

PORT, DB, STREAM_PORT = 3055, "tdm55e2e", 8195
ROBOT = "test.tdm55-yam"


def main() -> None:
    st = LocalStack(port=PORT, db=DB, root=Path(tempfile.gettempdir()) / "tdm-55-e2e")
    st.start_server()
    stream = None
    try:
        st.publish()
        st.make_identity("orchestrator")
        orch = st.client("orchestrator")
        orch.call_reducer("register_robot", ROBOT, "i2rt_yam", "test", json.dumps(
            {"stream": {"url": f"ws://127.0.0.1:{STREAM_PORT}/ws/cameras", "transport": "jpeg-ws/1",
                        "cams": ["top", "wrist"]}}))
        robot = st.sql(f"SELECT * FROM robot WHERE name = '{ROBOT}'")[0]
        orch.call_reducer("create_ticket", robot["id"], "test.tdm55 stream auth", 0.8, 1000, "")
        ticket = st.sql("SELECT * FROM ticket")[-1]
        orch.call_reducer("create_task", ticket["id"], "clean_teleop", 1000, json.dumps({"test": "tdm55"}))
        task = st.sql("SELECT * FROM task")[-1]

        from rdf_common.spacetime_client import SpacetimeClient

        def person(name: str):
            d = httpx.post(f"{st.host}/v1/identity").json()
            c = SpacetimeClient(st.host, DB, d["token"])
            c.call_reducer("register_contributor", name, "", ["laptop"], "contributor", 0)
            row = [r for r in st.sql("SELECT * FROM contributor") if r["identity"].lower().endswith(d["identity"].lower())][0]
            return d["identity"], d["token"], c, row["id"]

        me_id, me_tok, me, me_cid = person("test.tdm55 claimant")
        st_id, st_tok, _, _ = person("test.tdm55 stranger")
        me.call_reducer("claim_task", task["id"], me_cid)
        print("claimed task", task["id"], "as contributor", me_cid)

        # robot-client stream server for this robot (synthetic cameras)
        rc = ROOT / "services" / "robot-client"
        stream = subprocess.Popen(["uv", "run", "python", "-m", "rdf_robot_client.stream", "serve", "--synthetic",
                                   "--tunnel", "none", "--host", "127.0.0.1", "--port", str(STREAM_PORT),
                                   "--robot-name", ROBOT], cwd=rc, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4)

        os.environ.update({"SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": DB, "SPACETIME_TOKEN": st.token("orchestrator")})
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from rdf_orchestrator import stream_api

        app = FastAPI()
        app.include_router(stream_api.stream_router)
        c = TestClient(app)

        def ask(ident: str, tok: str):
            stream_api._tdm55_client = None  # fresh client per TestClient loop
            return c.post("/stream/token", json={"task_id": task["id"], "identity": ident, "spacetime_token": tok})

        r = ask(st_id, st_tok)
        print("stranger ->", r.status_code, r.json()["detail"])
        assert r.status_code == 403
        r = ask(me_id, st_tok)
        print("claimant id + stranger's token ->", r.status_code)
        assert r.status_code == 401
        r = ask(me_id, me_tok)
        assert r.status_code == 200, r.text
        g = r.json()
        print("claimant -> 200, ttl %.0fs, url %s" % (g["exp"] - time.time(), g["stream_url"]))

        out = subprocess.run(["uv", "run", "python", "-m", "rdf_robot_client.stream", "probe", g["stream_url"],
                              "--token", g["token"], "--seconds", "3"], cwd=rc, capture_output=True, text=True)
        res = json.loads(out.stdout)
        print("probe with minted token:", json.dumps(res["cams"]))
        assert res["cams"] and all(v["frames"] > 30 for v in res["cams"].values())

        me.call_reducer("release_task", task["id"])
        r = ask(me_id, me_tok)
        print("after release_task ->", r.status_code, r.json()["detail"])
        assert r.status_code == 403
        print("E2E OK")
    finally:
        if stream:
            stream.terminate()
        st.stop()


if __name__ == "__main__":
    main()
