"""Robot-side orchestrator discovery, end to end on a THROWAWAY local stack (nothing touches maincloud, the
Modal Volume, the user's ports or tunnels).

    cd services/orchestrator && nice -n 10 uv run python ../../spikes/orch-discovery/e2e.py [--no-tunnel]

  1. LocalStack SpacetimeDB on a free port, the TDM-58 media-store stand-in, the orchestrator with only the
     reviewer + payout handlers (dry-run drops/texts, blank Discord/Inkbox creds).
  2. Robot row, ticket, a clean_teleop task claimed by a contributor.
  3. Edge A (public_guard.py, no --web) on a local port; event_log ``orchestrator.endpoint`` -> A.
  4. A sim robot client (recorder CLI: SimArm + synthetic cameras) with NO ORCHESTRATOR_URL, only
     RDF_EDGE_SECRET: it discovers + proves A.
  5. "Backend restart": state=down, edge A killed, edge B behind a fresh cloudflared quick tunnel
     (--no-tunnel: a second local edge); ``orchestrator.endpoint`` -> B.
  6. The recording stops, the uploader signs through B (x-rdf-edge-secret), PUTs to the media store,
     create_episode; the orchestrator's reviewer ingests it (review_status leaves pending).
Exit 0 on success. Everything started here is stopped on exit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("ORCH_DISCOVERY_E2E_ROOT") or Path(tempfile.gettempdir()) / "rdf-orch-discovery-e2e")
os.environ["TDM28_ROOT"] = str(ROOT)
sys.path.insert(0, str(REPO / "tests" / "integration"))
sys.path.insert(0, str(REPO / "spikes" / "tdm-58-recording"))
sys.path.insert(0, str(REPO / "scripts" / "demo"))

from e2e_stack import SIGNING_KEY, LocalMediaStore  # noqa: E402
from harness import REVIEW_CONFIG, LocalStack, wait_for  # noqa: E402
from public_guard import edge_proof  # noqa: E402

USER_PORTS = {8028, 3737, 8791, 8792, 8765, 3767, 3028, 3030, 8030, 3747}
TRYCF = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def log(msg: str) -> None:
    print(f"[orch-discovery-e2e] {msg}", flush=True)


def free_port() -> int:
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if p not in USER_PORTS:
            return p


def get(url: str, timeout: float = 5.0, headers: dict | None = None) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception:  # noqa: BLE001
        return 0, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tunnel", action="store_true", help="edge B on a local port too (no cloudflared)")
    ap.add_argument("--stdb-port", type=int, default=0)
    a = ap.parse_args()
    stdb_port = a.stdb_port or free_port()
    assert stdb_port not in USER_PORTS
    st = LocalStack(stdb_port, "orchdisc")
    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    edge_secret = secrets.token_urlsafe(32)  # throwaway; never printed
    py = sys.executable
    ok = False
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        media = LocalMediaStore(st.root / "media-store")
        orch_port = free_port()
        env = {**st.env("orchestrator"), "RDF_REVIEW_CONFIG": str(REVIEW_CONFIG), "RDF_MEDIA_BACKEND": "modal",
               "RDF_MEDIA_STORE_URL": media.url, "RDF_MEDIA_SIGNING_KEY": SIGNING_KEY,
               "RDF_STREAM_TOKEN_SECRET": "e2e-stream-" + secrets.token_hex(12),
               "RDF_DROP_MODE": "dry_run", "RDF_DROP_DIR": str(st.root / "drops"), "RDF_TEXT_MODE": "dry_run",
               "DISCORD_BOT_TOKEN": "", "DISCORD_APPLICATION_ID": "", "INKBOX_API_KEY": "", "RDF_EDGE_SECRET": ""}
        st._spawn("orchestrator", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "orchestrator"),
                                   "python", str(REPO / "spikes" / "tdm-58-recording" / "orch_reviewer_only.py"),
                                   "--port", str(orch_port)], env=env)
        wait_for(lambda: json.loads(get(f"http://127.0.0.1:{orch_port}/healthz")[1] or "{}")
                 .get("subscription", {}).get("state") == "live", 120, "orchestrator live")
        orch_url = f"http://127.0.0.1:{orch_port}"
        log(f"orchestrator (reviewer only) on {orch_url}, SpacetimeDB {st.host}/{st.db}")

        robot = st.client("robot_client")
        robot.call_reducer("register_robot", "test.orchdisc-sim-yam", "sim", "orchdisc-sim", json.dumps({"sim": True}))
        rid = int(wait_for(lambda: (st.sql("SELECT * FROM robot WHERE name = 'test.orchdisc-sim-yam'") or [None])[0],
                           10, "robot")["id"])
        orch = st.client("orchestrator")
        orch.call_reducer("create_ticket", rid, "[SIM] orch discovery e2e: put the cup in the bin", 0.8, 5000, "")
        tid = int(st.newest("ticket")["id"])
        orch.call_reducer("create_task", tid, "clean_teleop", 1000, json.dumps({"instructions": "[SIM] e2e"}))
        task = int(st.newest("task", f"ticket_id = {tid}")["id"])
        from rdf_common.spacetime_client import SpacetimeClient, create_identity

        _i, ctoken = create_identity(st.host)
        contrib = SpacetimeClient(st.host, st.db, ctoken)
        contrib.call_reducer("register_contributor", "test.orchdisc operator", "", ["web"], "contributor", tid)
        cid = int(max((r for r in st.sql("SELECT * FROM contributor") if r["name"] == "test.orchdisc operator"),
                      key=lambda r: int(r["id"]))["id"])
        contrib.call_reducer("claim_task", task, cid)

        def edge(name: str, port: int, public_url: str):
            return st._spawn(name, [py, str(REPO / "scripts" / "demo" / "public_guard.py"), "--listen",
                                    f"127.0.0.1:{port}", "--orch", orch_url],
                             env={"RDF_PUBLIC_URL": public_url, "RDF_EDGE_SECRET": edge_secret,
                                  "RDF_PUBLIC_API_SECRET": "", "RDF_PUBLIC_ACCESS_CODE": ""})

        def publish(payload: dict) -> int:
            orch.call_reducer("append_event", "orchestrator", "orchestrator.endpoint", json.dumps(payload))
            return int(st.newest("event_log", "type = 'orchestrator.endpoint'")["id"])

        # edge A: local
        pa = free_port()
        url_a = f"http://127.0.0.1:{pa}"
        edge("edge-a", pa, url_a)
        wait_for(lambda: get(f"{url_a}/_rdf/healthz")[0] == 200, 20, "edge A")
        ida = publish({"url": url_a, "robot_url": None, "tunnel": "local", "started_at": int(time.time())})
        log(f"published orchestrator.endpoint #{ida} -> {url_a} (edge A)")

        # edge B: behind a quick tunnel (started now; published after the 'restart')
        pb = free_port()
        if a.no_tunnel:
            url_b = f"http://localhost:{pb}"
        else:
            cf = shutil.which("cloudflared")
            if not cf:
                raise RuntimeError("cloudflared not found (or pass --no-tunnel)")
            st._spawn("tunnel-b", [cf, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{pb}"])
            url_b = wait_for(lambda: (TRYCF.findall((st.logs / "tunnel-b.log").read_text(errors="replace")) or [None])[-1],
                             60, "quick tunnel URL")
        edge("edge-b", pb, url_b)
        nonce = secrets.token_urlsafe(18)
        wait_for(lambda: json.loads(get(f"{url_b}/_rdf/proof?nonce={nonce}", 8)[1] or "{}").get("proof")
                 == edge_proof(edge_secret, nonce, url_b), 120, f"{url_b}/_rdf/proof (through Cloudflare)", every=2)
        log(f"edge B ready at {url_b}")

        # the sim robot client: discovery only (no ORCHESTRATOR_URL)
        rec_out = st.root / "recorder.out.json"
        renv = {"RDF_EDGE_SECRET": edge_secret, "ORCHESTRATOR_URL": "", "RDF_ORCH_DISCOVERY_S": "2",
                "SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db, "RDF_EPISODES_DIR": str(st.root / "episodes")}
        with open(rec_out, "wb") as out:
            import subprocess

            robot_proc = subprocess.Popen(
                ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "python", "-m",
                 "rdf_robot_client.recorder", "record", "--seconds", "14", "--ticket", str(tid), "--task", str(task),
                 "--contributor", str(cid), "--kind", "clean_teleop", "--upload", "--host", st.host, "--db", st.db,
                 "--token-file", str(st.tokens / "robot_client.token"), "--root", str(st.root / "episodes"),
                 "--upload-timeout", "180"],
                stdout=out, stderr=open(st.logs / "robot.log", "wb"), cwd=REPO, env={**os.environ, **renv},
                start_new_session=True)
        st.procs["robot"] = robot_proc
        rlog = st.logs / "robot.log"
        wait_for(lambda: url_a in rlog.read_text(errors="replace") and "orchestrator endpoint discovered"
                 in rlog.read_text(errors="replace"), 60, "the robot to discover edge A", every=0.5)
        log("robot discovered edge A")

        # 'restart' the backend: down row, edge A gone, a new tunnel URL published
        publish({"url": None, "state": "down", "previous_url": url_a, "stopped_at": int(time.time())})
        p = st.procs.pop("edge-a")
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(10)
        idb = publish({"url": url_b, "robot_url": None, "tunnel": "local" if a.no_tunnel else "quick",
                       "started_at": int(time.time())})
        log(f"restart: edge A stopped, published orchestrator.endpoint #{idb} -> {url_b}")

        last_hb = time.monotonic()
        while robot_proc.poll() is None and not stop["flag"]:
            if time.monotonic() - last_hb > 15:
                contrib.call_reducer("heartbeat_claim", task)
                last_hb = time.monotonic()
            time.sleep(0.5)
        res = json.loads(rec_out.read_text() or "{}")
        up = res.get("upload") or {}
        text = rlog.read_text(errors="replace")
        assert edge_secret not in text and edge_secret not in rec_out.read_text(), "the edge secret was logged"
        found = [json.loads(line) for line in text.splitlines() if line.startswith("{") and
                 '"orchestrator endpoint discovered"' in line]
        log("robot discovery log lines:\n    " + "\n    ".join(json.dumps(f) for f in found))
        assert [f["url"] for f in found][-1] == url_b, "the robot did not move to edge B"
        assert up.get("state") == "done", f"upload not done: {up}"
        ep_id = int(up["episode_id"])
        ep = wait_for(lambda: next((r for r in st.sql(f"SELECT * FROM episode WHERE id = {ep_id}")
                                    if r["review_status"] != "pending"), None), 120, "the reviewer verdict", every=1)
        files = sorted(p.name for p in (st.root / "media-store").rglob("*") if p.is_file())
        log(f"episode {ep_id} (task {task}) storage {ep['storage_uri']} review_status={ep['review_status']}; "
            f"media store files: {files}")
        ok = True
        log("PASS")
    except Exception as e:  # noqa: BLE001
        log(f"FAIL: {e} (logs in {st.logs})")
    finally:
        st.stop()
        log("stopped everything this run started")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
