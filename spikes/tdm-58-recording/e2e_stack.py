"""TDM-58 end-to-end proof stack (laptop, nothing touches maincloud or the Modal Volume).

  1. Throwaway SpacetimeDB on 127.0.0.1:$PORT (TDM-28 LocalStack, data under $TMPDIR/tdm-58/).
  2. A local stand-in for the Modal media store (same HMAC /o/{key}?exp&sig protocol, files on disk).
  3. The orchestrator app with only the TDM-25 reviewer + payout handlers (orch_reviewer_only.py),
     reviewing with tests/integration/review_fixtures.yaml (fixtures allowed, Claude judgement off).
  4. A robot row, a ticket, a clean_teleop task and a recovery_takeover task, both claimed by a
     contributor identity this script keeps heartbeating (60 s arm-control leases).
  5. sim_control_robot.py: SimArm + synthetic cameras + camera stream + /control.
  6. Prints the dev-page URLs (apps/web /dev/tdm58-recording) with a stream token for each task.

    cd services/orchestrator && uv run python ../../spikes/tdm-58-recording/e2e_stack.py --port 3058
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import signal
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
os.environ.setdefault("TDM28_ROOT", str(Path(tempfile.gettempdir()) / "tdm-58"))
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import REVIEW_CONFIG, LocalStack, wait_for  # noqa: E402

SIGNING_KEY = "tdm58-local-media-signing-key-0123456789"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class LocalMediaStore:
    """services/media-store stand-in: PUT/GET /o/{key}?exp&sig (HMAC-SHA256 of 'METHOD\\nkey\\nexp')."""

    def __init__(self, root: Path, key: str = SIGNING_KEY) -> None:
        root.mkdir(parents=True, exist_ok=True)
        store_dir, signing = root, key

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _key(self, method: str) -> str | None:
                u = urlsplit(self.path)
                if not u.path.startswith("/o/"):
                    return None
                q = parse_qs(u.query)
                k = unquote(u.path[3:])
                try:
                    exp = int(q["exp"][0])
                    sig = q["sig"][0]
                except (KeyError, ValueError):
                    return None
                want = hmac.new(signing.encode(), f"{method}\n{k}\n{exp}".encode(), hashlib.sha256).hexdigest()
                return k if hmac.compare_digest(want, sig) and exp > time.time() and ".." not in k else None

            def do_PUT(self):
                k = self._key("PUT")
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not k:
                    self.send_response(403)
                    self.end_headers()
                    return
                p = store_dir / k
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def do_GET(self):
                k = self._key("GET")
                p = store_dir / (k or "")
                if not k or not p.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                data = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3058)
    ap.add_argument("--db", default="tdm58rec")
    ap.add_argument("--orch-port", type=int, default=0)
    ap.add_argument("--stream-port", type=int, default=0)
    ap.add_argument("--control-port", type=int, default=0)
    ap.add_argument("--web", default="http://127.0.0.1:3758")
    a = ap.parse_args()
    orch_port = a.orch_port or free_port()
    stream_port = a.stream_port or free_port()
    control_port = a.control_port or free_port()

    st = LocalStack(a.port, a.db)
    stop = threading.Event()

    def _term(*_):
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    secret = "tdm58-local-stream-secret-" + os.urandom(8).hex()
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        media = LocalMediaStore(st.root / "media-store")
        print(f"[tdm58] media store {media.url} (files under {st.root / 'media-store'})", flush=True)

        env = {**st.env("orchestrator"), "RDF_REVIEW_CONFIG": str(REVIEW_CONFIG), "RDF_MEDIA_BACKEND": "modal",
               "RDF_MEDIA_STORE_URL": media.url, "RDF_MEDIA_SIGNING_KEY": SIGNING_KEY,
               "RDF_STREAM_TOKEN_SECRET": secret}
        st._spawn("orchestrator", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "orchestrator"),
                                   "python", str(HERE / "orch_reviewer_only.py"), "--port", str(orch_port)], env=env)
        import urllib.request

        wait_for(lambda: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{orch_port}/healthz", timeout=1).read())
                 ["subscription"]["state"] == "live", 90, "orchestrator live")
        print(f"[tdm58] orchestrator (reviewer + payouts only) on :{orch_port}", flush=True)

        robot = st.client("robot_client")
        robot.call_reducer("register_robot", "test.tdm58-sim-yam", "sim", "tdm58-sim", json.dumps({"sim": True}))
        rrow = wait_for(lambda: (st.sql("SELECT * FROM robot WHERE name = 'test.tdm58-sim-yam'") or [None])[0], 10, "robot")
        robot_id = int(rrow["id"])
        orch = st.client("orchestrator")
        orch.call_reducer("create_ticket", robot_id, "[SIM] tdm58: put the cup in the bin", 0.8, 5000, "")
        ticket_id = int(st.newest("ticket")["id"])
        orch.call_reducer("create_task", ticket_id, "clean_teleop", 1000,
                          json.dumps({"instructions": "[SIM] tdm58 clean demo"}))
        orch.call_reducer("create_task", ticket_id, "recovery_takeover", 500,
                          json.dumps({"instructions": "[SIM] tdm58 takeover"}))
        tasks = sorted(st.sql(f"SELECT * FROM task WHERE ticket_id = {ticket_id}"), key=lambda r: int(r["id"]))

        from rdf_common.spacetime_client import SpacetimeClient, create_identity

        _ident, ctoken = create_identity(st.host)
        contrib = SpacetimeClient(st.host, st.db, ctoken)
        contrib.call_reducer("register_contributor", "test.tdm58 operator", "", ["web"], "contributor", ticket_id)
        cid = int(max((r for r in st.sql("SELECT * FROM contributor") if r["name"] == "test.tdm58 operator"),
                      key=lambda r: int(r["id"]))["id"])
        # One live arm-control lease per robot: claim the clean task now, the takeover task once the
        # clean episode has its verdict (the clean task is completed first).
        contrib.call_reducer("claim_task", int(tasks[0]["id"]), cid)
        held = [int(tasks[0]["id"])]

        os.environ["RDF_STREAM_TOKEN_SECRET"] = secret
        from rdf_common.stream_token import mint

        st._spawn("sim-robot", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"),
                                "python", str(HERE / "sim_control_robot.py"), "--host", st.host, "--db", st.db,
                                "--token-file", str(st.tokens / "robot_client.token"),
                                "--orchestrator-url", f"http://127.0.0.1:{orch_port}", "--robot-id", str(robot_id),
                                "--stream-port", str(stream_port), "--control-port", str(control_port),
                                "--episodes-dir", str(st.root / "episodes")],
                  env={"RDF_STREAM_TOKEN_SECRET": secret, "SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db})
        wait_for(lambda: socket.create_connection(("127.0.0.1", control_port), timeout=0.5).close() or True, 60,
                 "sim robot control port")

        from urllib.parse import quote

        info = {"spacetime": st.host, "db": st.db, "ticket_id": ticket_id, "robot_id": robot_id, "contributor_id": cid,
                "orchestrator": f"http://127.0.0.1:{orch_port}", "logs": str(st.logs), "pages": {}}
        for t in tasks:
            tok, _ = mint(secret, robot_id=robot_id, task_id=int(t["id"]), sub=f"contributor:{cid}", ttl_s=6 * 3600)
            q = (f"control={quote(f'ws://127.0.0.1:{control_port}/control')}&cams={quote(f'ws://127.0.0.1:{stream_port}/ws/cameras')}"
                 f"&token={tok}&task={t['id']}&kind={t['type']}")
            info["pages"][t["type"]] = f"{a.web}/dev/tdm58-recording?{q}"
        (st.root / "tdm58-stack.json").write_text(json.dumps(info, indent=2))
        print("[tdm58] STACK READY " + json.dumps(info), flush=True)
        print(f"[tdm58] web: cd apps/web && NEXT_DIST_DIR=.next-tdm58 NEXT_PUBLIC_SPACETIME_HOST={st.host} "
              f"NEXT_PUBLIC_SPACETIME_DB_NAME={st.db} pnpm exec next dev --port {a.web.rsplit(':', 1)[1]}", flush=True)

        last_hb = 0.0
        while not stop.is_set():
            if time.monotonic() - last_hb > 15:
                for tid in held:
                    try:
                        contrib.call_reducer("heartbeat_claim", tid)
                    except Exception as e:  # noqa: BLE001
                        print(f"[tdm58] heartbeat {tid} failed: {e}", flush=True)
                last_hb = time.monotonic()
            if len(held) == 1 and held[0] == int(tasks[0]["id"]):
                eps = st.sql(f"SELECT * FROM episode WHERE task_id = {held[0]}")
                if any(e["review_status"] in ("accepted", "rejected") for e in eps):
                    try:
                        contrib.call_reducer("complete_task", held[0], json.dumps({"tdm58": "clean demo recorded"}))
                        contrib.call_reducer("claim_task", int(tasks[1]["id"]), cid)
                        held = [int(tasks[1]["id"])]
                        print(f"[tdm58] clean episode reviewed; completed task {tasks[0]['id']}, "
                              f"claimed takeover task {tasks[1]['id']}", flush=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"[tdm58] switch to takeover task failed: {e}", flush=True)
            dead = [n for n, p in st.procs.items() if p.poll() is not None]
            if dead:
                print(f"[tdm58] process exited: {dead}; logs {st.logs}", flush=True)
                break
            stop.wait(1.0)
    finally:
        st.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
