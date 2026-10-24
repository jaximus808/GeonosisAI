"""TDM-57 test stack on a throwaway local SpacetimeDB (never maincloud).

    cd services/orchestrator && uv run python ../../spikes/tdm-57-lease/stack.py up

1. ``spacetime start`` on 127.0.0.1:3057, data under $TMPDIR/tdm-57/run-3057 (wiped every boot), and
   publishes the module as DB ``tdm57lease`` (TDM-28's LocalStack).
2. Mints an ``orchestrator`` service identity, registers robot ``test.tdm57-yam`` whose stream URL
   points at the [MOCK] control server (ws://127.0.0.1:8957/ws/cameras), one ticket and two
   ``clean_teleop`` tasks.
3. Serves only the two routes under test on :8857: TDM-55 ``POST /stream/token`` and TDM-57
   ``POST /tasks/{id}/revoke`` (operator key in $TMPDIR/tdm-57/run-3057/operator.key, never printed).

Prints the env lines for the web dev server and the mock control server. Tokens stay in files.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
os.environ.setdefault("TDM28_ROOT", str(Path(tempfile.gettempdir()) / "tdm-57"))

from harness import LocalStack, _log  # noqa: E402

PORT, DB, API_PORT, CONTROL_PORT = 3057, "tdm57lease", 8857, 8957


def seed(st: LocalStack) -> dict:
    c = st.client("orchestrator")
    stream = {"url": f"ws://127.0.0.1:{CONTROL_PORT}/ws/cameras", "http": f"http://127.0.0.1:{CONTROL_PORT}",
              "transport": "jpeg-ws/1", "cams": [], "mock": True}
    c.call_reducer("register_robot", "test.tdm57-yam", "yam", "mock-tdm57", json.dumps({"stream": stream}))
    robot = c.sql("SELECT * FROM robot WHERE name = 'test.tdm57-yam'")[0]
    c.call_reducer("robot_heartbeat", int(robot["id"]), "online")
    c.call_reducer("create_ticket", int(robot["id"]), "test.tdm57 teleop lease", 0.8, 10_000, "")
    ticket = max(c.sql("SELECT * FROM ticket"), key=lambda r: int(r["id"]))
    for i in range(2):
        c.call_reducer("create_task", int(ticket["id"]), "clean_teleop", 0,
                       json.dumps({"instructions": f"test.tdm57 drive #{i + 1}"}))
    tasks = [int(t["id"]) for t in c.sql("SELECT * FROM task")]
    return {"robot_id": int(robot["id"]), "ticket_id": int(ticket["id"]), "task_ids": tasks}


def main() -> None:
    st = LocalStack(port=PORT, db=DB)
    st.start_server()
    st.publish()
    st.make_identity("orchestrator")
    info = seed(st)
    key_file = st.root / "operator.key"
    key_file.write_text(secrets.token_urlsafe(24))
    os.chmod(key_file, 0o600)
    (st.root / "seed.json").write_text(json.dumps(info))
    _log(f"seeded {info}")

    os.environ.update(st.env("orchestrator"))
    os.environ["RDF_OPERATOR_KEY"] = key_file.read_text()
    os.environ.pop("RDF_STREAM_URL", None)
    print(f"# web:     NEXT_PUBLIC_SPACETIME_HOST={st.host} NEXT_PUBLIC_SPACETIME_DB_NAME={DB} "
          f"RDF_ORCHESTRATOR_URL=http://127.0.0.1:{API_PORT} RDF_OPERATOR_KEY=\"$(cat {key_file})\"", flush=True)
    print(f"# control: --robot-id {info['robot_id']} --stdb-host {st.host} --stdb-db {DB} --port {CONTROL_PORT}",
          flush=True)

    import uvicorn
    from fastapi import FastAPI

    from rdf_orchestrator.stream_api import stream_router
    from rdf_orchestrator.tdm57_control_api import tdm57_control_router

    app = FastAPI(title="tdm57 test api")
    app.include_router(stream_router)
    app.include_router(tdm57_control_router)
    try:
        uvicorn.run(app, host="127.0.0.1", port=API_PORT, log_level="info")
    finally:
        st.stop()


if __name__ == "__main__":
    main()
