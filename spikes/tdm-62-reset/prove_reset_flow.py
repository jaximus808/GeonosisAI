"""TDM-62 end-to-end SIM proof of the reset task flow, on a THROWAWAY local stack (never maincloud).

  eval trial ends -> dispatcher posts a $2 reset task (TDM-26) -> nobody claims -> re-paged
  (dispatch.reset_escalated, TDM-62) -> a contributor claims it -> robot-camera scene photo
  (TDM-55 snapshot via POST /resets/{id}/photo) -> complete_task -> reserve_task_payment
  ($2 payment row, Stripe TEST MODE, not sent) + resume_policy -> robot leaves waiting_reset and
  the next trial starts (measured: seconds from complete_task to eval.trial_started).

Pieces (all local, all SIM):
  * SpacetimeDB on a free port, db tdm62reset, data under $TMPDIR/tdm-62/ (TDM-28 harness)
  * a local media store (PUT/GET/list, same HMAC as the Modal one) behind the REAL reset API app
    (``rdf_orchestrator.reset_api:app``: /resets/*, /stream/token, /uploads/*, /media/*)
  * a synthetic TDM-55 camera stream server (no tunnel) the reset API snapshots from
  * the TDM-50 fake policy server + the real robot client ``--sim --sim-policy --cameras synthetic``
    (TDM-51 trial runner; no operator verdicts, so every trial is a SIM failure "outcome not confirmed")
  * in this process: a bus with ONLY the TDM-26 dispatcher, the TDM-67 eval handlers and the TDM-62
    reset watch. EVAL_SOLO_MODE=0: resets wait for a person.

Who does the resets:
  default            this script, as a contributor over HTTP (claim, camera photo, complete)
  --web N            the first N resets are left for a browser: it starts ``next dev`` on --web-port
                     pointed at this stack and prints the /reset/<id> URL for each, then waits
  --hold S           keep everything up S seconds after the run (screenshots)

    cd services/orchestrator && uv run python ../../spikes/tdm-62-reset/prove_reset_flow.py [--web 1]

Writes spikes/tdm-62-reset/tmp/proof.json.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
OUT = REPO / "spikes" / "tdm-62-reset" / "tmp"
SIGNING_KEY = "tdm62-proof-signing-key-0123456789"
STREAM_SECRET = "tdm62-proof-stream-secret-0123456789"
ROBOT = "test.tdm62-sim"
TRIALS = 3


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def log(msg: str, **kw) -> None:
    print(f"[tdm62-proof] {msg}" + (f" {json.dumps(kw, default=str)}" if kw else ""), file=sys.stderr, flush=True)


def wait_for(pred, timeout: float, what: str, every: float = 0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = pred()
        except Exception:  # noqa: BLE001
            v = None
        if v:
            return v
        time.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


class LocalMediaStore:
    """Stand-in for the Modal media store: PUT/GET /o/{key}?exp&sig and GET /list (bearer key)."""

    def __init__(self, root: Path) -> None:
        self.dir = root
        self.dir.mkdir(parents=True, exist_ok=True)
        store = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")

            def _auth(self, method):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                k = unquote(u.path[len("/o/"):])
                try:
                    want = hmac.new(SIGNING_KEY.encode(), f"{method}\n{k}\n{int(q['exp'][0])}".encode(),
                                    hashlib.sha256).hexdigest()
                    return k if hmac.compare_digest(want, q["sig"][0]) else None
                except (KeyError, ValueError):
                    return None

            def do_PUT(self):
                k = self._auth("PUT")
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if not k:
                    self.send_response(403)
                    self.end_headers()
                    return
                p = store.dir / k
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(body)
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def do_GET(self):
                u = urlsplit(self.path)
                if u.path == "/list":
                    if self.headers.get("Authorization") != f"Bearer {SIGNING_KEY}":
                        self.send_response(401)
                        self.end_headers()
                        return
                    prefix = (parse_qs(u.query).get("prefix") or [""])[0]
                    objs = [{"key": str(p.relative_to(store.dir)), "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)}
                            for p in store.dir.rglob("*") if p.is_file() and str(p.relative_to(store.dir)).startswith(prefix)]
                    body = json.dumps({"objects": objs}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                k = self._auth("GET")
                p = store.dir / k if k else None
                if not p or not p.is_file():
                    self.send_response(404 if k else 403)
                    self.end_headers()
                    return
                data = p.read_bytes()
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "image/jpeg" if p.suffix == ".jpg" else "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def spawn(procs: list, logs: Path, name: str, cmd: list[str], env: dict, cwd: Path | None = None) -> subprocess.Popen:
    p = subprocess.Popen(cmd, cwd=cwd or REPO, env=env, stdin=subprocess.DEVNULL,
                         stdout=open(logs / f"{name}.log", "wb"), stderr=subprocess.STDOUT, start_new_session=True)
    procs.append(p)
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", type=int, default=0, help="leave the first N resets for a browser")
    ap.add_argument("--web-port", type=int, default=3762)
    ap.add_argument("--hold", type=float, default=0.0)
    ap.add_argument("--escalate-s", type=float, default=12.0)
    a = ap.parse_args()

    from harness import LocalStack  # TDM-28
    from rdf_common.spacetime_client import SpacetimeClient
    import httpx

    root = Path(tempfile.gettempdir()) / "tdm-62"
    OUT.mkdir(parents=True, exist_ok=True)
    st = LocalStack(port=free_port(), db="tdm62reset", root=root)
    assert "maincloud" not in st.host
    procs: list[subprocess.Popen] = []
    loop = asyncio.new_event_loop()
    bus = None
    report: dict = {"label": "SIM: SimArm + synthetic cameras + fake policy; trials are SIM failures (no verdicts)",
                    "resets": []}
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        orch = SpacetimeClient(st.host, st.db, st.token("orchestrator"))
        logs = st.logs

        # ---- fake policy + the real robot client (sim arm, TDM-51 trial runner, TDM-55 stream on LAN)
        api_port, robot_stream_port = free_port(), free_port()
        api = f"http://127.0.0.1:{api_port}"
        pol_port = free_port()
        spawn(procs, logs, "fake-policy",
              ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "python", "-c",
               "import time; from rdf_robot_client.fake_policy_server import FakePolicyServer, jitter_schedule; "
               f"FakePolicyServer(schedule=jitter_schedule(120, 20), port={pol_port}).start(); time.sleep(1e9)"],
              dict(os.environ))
        wait_for(lambda: httpx.get(f"http://127.0.0.1:{pol_port}/health", timeout=1).status_code == 200, 60, "fake policy")
        spawn(procs, logs, "robot-client",
              ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "rdf-robot-client",
               "--sim", "--sim-policy", "--cameras", "synthetic", "--name", ROBOT, "--host", st.host, "--db", st.db,
               "--token-file", str(st.tokens / "robot_client.token"), "--heartbeat-s", "1"],
              {**os.environ, "ORCHESTRATOR_URL": api, "POLICY_BACKEND": "spike",
               "POLICY_ENDPOINT_URL": f"http://127.0.0.1:{pol_port}", "RDF_EPISODES_DIR": str(st.root / "episodes"),
               "RDF_EVAL_OUTCOME_TIMEOUT_S": "1", "RDF_EVAL_UPLOAD_WAIT_S": "30",
               "RDF_STREAM": "none", "RDF_STREAM_PORT": str(robot_stream_port), "RDF_STREAM_TOKEN_SECRET": STREAM_SECRET})
        robot = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM robot WHERE name = '{ROBOT}'")
                                       if r["status"] == "online"), None), 90, "sim robot online")
        rid = int(robot["id"])
        time.sleep(2.0)
        robot = orch.sql(f"SELECT * FROM robot WHERE id = {rid}")[0]
        stream_info = (json.loads(robot.get("camera_config_json") or "{}") or {}).get("stream") or {}
        api_env = {**os.environ, **st.env("orchestrator"), "RDF_MEDIA_BACKEND": "modal",
                   "RDF_MEDIA_SIGNING_KEY": SIGNING_KEY, "RDF_MEDIA_BUCKET": "rdf-media",
                   "RDF_STREAM_TOKEN_SECRET": STREAM_SECRET,
                   "RDF_WEB_ORIGINS": f"http://127.0.0.1:{a.web_port},http://localhost:{a.web_port}"}
        if not stream_info.get("url"):  # robot client without the TDM-52 stream wiring: a separate synthetic server
            cam_port = free_port()
            spawn(procs, logs, "camera-stream",
                  ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "python", "-m",
                   "rdf_robot_client.stream", "serve", "--synthetic", "--tunnel", "none", "--host", "127.0.0.1",
                   "--port", str(cam_port), "--robot-name", ROBOT, "--fps", "10"],
                  {**os.environ, "RDF_STREAM_TOKEN_SECRET": STREAM_SECRET})
            wait_for(lambda: httpx.get(f"http://127.0.0.1:{cam_port}/healthz", timeout=1).status_code == 200, 60, "camera stream")
            api_env["RDF_STREAM_URL"] = f"ws://127.0.0.1:{cam_port}/ws/cameras"
        log("robot online", robot_id=rid, stream=stream_info.get("url") or api_env.get("RDF_STREAM_URL"))
        store = LocalMediaStore(st.root / "media-store")
        api_env["RDF_MEDIA_STORE_URL"] = store.url
        spawn(procs, logs, "reset-api", ["uv", "run", "--quiet", "uvicorn", "rdf_orchestrator.reset_api:app",
                                         "--host", "127.0.0.1", "--port", str(api_port)],
              api_env, cwd=REPO / "services" / "orchestrator")
        wait_for(lambda: httpx.get(f"{api}/media/healthz", timeout=1).status_code == 200, 60, "reset API")

        # ---- web (optional): next dev pointed at this stack, own dist dir
        web = f"http://127.0.0.1:{a.web_port}"
        if a.web:
            spawn(procs, logs, "web", ["pnpm", "exec", "next", "dev", "--port", str(a.web_port), "--hostname", "127.0.0.1"],
                  {**os.environ, "NEXT_PUBLIC_SPACETIME_HOST": st.host, "NEXT_PUBLIC_SPACETIME_DB_NAME": st.db,
                   "RDF_ORCHESTRATOR_URL": api, "NEXT_PUBLIC_ORCHESTRATOR_URL": api, "NEXT_DIST_DIR": ".next-tdm62"},
                  cwd=REPO / "apps" / "web")
            wait_for(lambda: httpx.get(f"{web}/marketplace", timeout=60).status_code == 200, 240, "web dev server", every=2)
            log("web up", url=f"{web}/marketplace")

        # ---- orchestrator side in-process: dispatcher + eval + reset watch, humans do the resets
        os.environ.update({"EVAL_SOLO_MODE": "0", "EVAL_TRIAL_TIMEOUT_S": "5", "EVAL_WATCHDOG_S": "2",
                           "EVAL_ROBOT_STALE_S": "30", "POLICY_SERVER_URL": "", "RDF_RESET_ESCALATE_S": str(a.escalate_s),
                           **st.env("orchestrator")})
        from rdf_orchestrator import dispatcher as dsp
        from rdf_orchestrator import evaluate as evl
        from rdf_orchestrator.bus import Bus, Registry
        from rdf_orchestrator.handlers import dispatcher as h_dsp
        from rdf_orchestrator.handlers import evaluate as h_evl
        from rdf_orchestrator.handlers import resets as h_resets
        from rdf_orchestrator.stdb import SpacetimeClient as BusStdb

        writer = lambda: SpacetimeClient(st.host, st.db, st.token("orchestrator"))  # noqa: E731
        evl.make_writer = dsp.make_writer = writer
        reg = Registry()
        h_dsp.register(reg)
        h_evl.register(reg, None)
        h_resets.register(reg)
        reg.watch("event_log")  # reset watch reads its own escalation rows back (restart-safe dedup)
        bus = Bus(reg, BusStdb(host=st.host, db=st.db, token=st.token("orchestrator"), max_backoff=2.0), workers=4)
        threading.Thread(target=loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(bus.start(), loop).result(15)
        wait_for(lambda: bus.state == "live" and bus.get("robot", rid), 30, "bus live")
        ev = evl.get_evaluator(bus)

        def events(after: int = 0) -> list[dict]:
            return sorted(orch.sql(f"SELECT * FROM event_log WHERE id > {after}"), key=lambda r: r["id"])

        # ---- a contributor over HTTP (the scripted person at the table)
        def make_contributor(name: str):
            req = urllib.request.Request(f"{st.host}/v1/identity", data=b"", method="POST")
            d = json.loads(urllib.request.urlopen(req, timeout=10).read())
            c = SpacetimeClient(st.host, st.db, d["token"])
            c.call_reducer("register_contributor", name, "", ["phone"], "contributor", 0)
            row = wait_for(lambda: next((r for r in orch.sql("SELECT * FROM contributor")
                                         if str(r.get("identity") or "").lower().removeprefix("0x") == d["identity"].lower()), None),
                           10, "contributor row")
            return c, d["identity"], d["token"], int(row["id"])

        person, person_ident, person_tok, person_id = make_contributor("[SIM] tdm62 person")

        # ---- ticket + eval (this script stands in for the flywheel's EvalStep)
        orch.call_reducer("create_ticket", rid, "test.tdm62: put the cube in the bowl [SIM]", 0.8, 5000,
                          json.dumps({"eval_trials": TRIALS, "hyperparams": {"prompt": "put the cube in the bowl"}}))
        tid = int(max(orch.sql("SELECT * FROM ticket"), key=lambda r: int(r["id"]))["id"])
        orch.call_reducer("set_ticket_status", tid, "baseline", "test.tdm62 proof (stands in for the flywheel)")
        wait_for(lambda: (bus.get("ticket", tid) or {}).get("status") == "baseline", 10, "ticket in mirror")
        log("ensure_eval", outcome=ev.ensure_eval(tid, why="tdm62 proof"))
        run = wait_for(lambda: next(iter(orch.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}")), None), 20, "eval_run")
        run_id = int(run["id"])
        report.update(ticket_id=tid, robot_id=rid, eval_run_id=run_id, web=f"{web}/marketplace" if a.web else None)

        handled: set[int] = set()
        n = 0
        while True:
            cur = orch.sql(f"SELECT * FROM eval_run WHERE id = {run_id}")[0]
            resets = sorted((t for t in orch.sql(f"SELECT * FROM task WHERE ticket_id = {tid}") if t["type"] == "reset"),
                            key=lambda t: int(t["id"]))
            todo = [t for t in resets if int(t["id"]) not in handled and t["status"] in ("open", "claimed", "completed")]
            if cur["status"] != "running" and not [t for t in todo if t["status"] != "completed"]:
                break
            if not todo:
                time.sleep(0.3)
                continue
            task = todo[0]
            task_id = int(task["id"])
            handled.add(task_id)
            n += 1
            rr: dict = {"task_id": task_id, "price_cents": task["price_cents"], "by": "script"}
            req_ev = next((json.loads(e["payload_json"]) for e in events() if e["type"] == "dispatch.reset_requested"
                           and json.loads(e["payload_json"]).get("task_id") == task_id), {})
            rr["after_trial_index"] = req_ev.get("trial_index")
            rr["robot_waiting"] = bool(wait_for(lambda: orch.sql(f"SELECT * FROM robot WHERE id = {rid}")[0]["status"] == "paused",
                                                30, "robot paused (waiting_reset)")) if (req_ev.get("trial_index") or 0) < TRIALS - 1 else None
            if n <= a.web:
                rr["by"] = "browser"
                log("RESET FOR THE BROWSER", url=f"{web}/reset/{task_id}", task_id=task_id)
                done = wait_for(lambda: next((t for t in orch.sql(f"SELECT * FROM task WHERE id = {task_id}")
                                              if t["status"] in ("completed", "cancelled", "expired")), None), 900,
                                f"browser to finish reset {task_id}", every=0.5)
            else:
                if n == 1 or (a.web and n == a.web + 1):  # show the escalation once: let it sit unclaimed
                    esc = wait_for(lambda: next((json.loads(e["payload_json"]) for e in events()
                                                 if e["type"] == "dispatch.reset_escalated"
                                                 and json.loads(e["payload_json"]).get("task_id") == task_id), None),
                                   a.escalate_s + 20, "reset escalation")
                    rr["escalation"] = esc
                    log("escalated (nobody claimed)", **{k: esc[k] for k in ("level", "waited_s", "note")})
                person.call_reducer("claim_task", task_id, person_id)
                t0 = time.time()
                r = httpx.post(f"{api}/resets/{task_id}/photo", json={"identity": person_ident, "spacetime_token": person_tok,
                                                                      "source": "camera"}, timeout=10)
                rr["photo"] = {"status": r.status_code, "ms": round((time.time() - t0) * 1000),
                               **({k: r.json().get(k) for k in ("storage_uri", "cam", "bytes")} if r.status_code == 200 else {"detail": r.text[:200]})}
                person.call_reducer("complete_task", task_id, json.dumps({"source": "tdm62_proof", "done": "reset",
                                                                          "photo_uris": [rr["photo"].get("storage_uri")]}))
                done = orch.sql(f"SELECT * FROM task WHERE id = {task_id}")[0]
            done_us = int(done.get("completed_at") or 0)
            rr["status"] = done["status"]
            # payment + resume + next trial
            pay = wait_for(lambda: next((p for p in orch.sql(f"SELECT * FROM payment WHERE ticket_id = {tid}")
                                         if p.get("batch_ref") == f"task:{task_id}"), None), 10, "reset payment row")
            rr["payment"] = {k: pay[k] for k in ("id", "amount_cents", "status", "contributor_id", "batch_ref")}
            resume = wait_for(lambda: next((c for c in orch.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid}")
                                            if c["type"] == "resume_policy" and json.loads(c["payload_json"]).get("task_id") == task_id), None),
                              10, "resume_policy")
            rr["resume_policy"] = {"id": resume["id"], "issued_after_done_s": round((int(resume["issued_at"]) - done_us) / 1e6, 2)}
            nxt_idx = (rr["after_trial_index"] or 0) + 1
            if nxt_idx < TRIALS:
                started = wait_for(lambda: next((e for e in events() if e["type"] == "eval.trial_started"
                                                 and json.loads(e["payload_json"]).get("eval_run_id") == run_id
                                                 and json.loads(e["payload_json"]).get("index") == nxt_idx), None),
                                   30, f"trial {nxt_idx} started")
                rr["next_trial"] = {"index": nxt_idx, "started_after_done_s": round((int(started["ts"]) - done_us) / 1e6, 2)}
                states = [(int(e["ts"]), json.loads(e["payload_json"])) for e in events() if e["type"] == "robot.state_changed"
                          and int(e["ts"]) >= done_us]
                rr["robot_after_done"] = [f"{p['from']}->{p['to']}" for _, p in states[:3]]
                rel = next((ts for ts, p in states if p["from"] == "waiting_reset"), None)
                rr["robot_released_after_done_s"] = round((rel - done_us) / 1e6, 2) if rel else None
            else:
                rr["next_trial"] = None  # the reset after the last trial readies the scene for the next eval
                rr["resume_policy_status"] = orch.sql(f"SELECT * FROM robot_command WHERE id = {resume['id']}")[0]["status"]
            report["resets"].append(rr)
            log("reset done", **rr)

        fin = orch.sql(f"SELECT * FROM eval_run WHERE id = {run_id}")[0]
        report["eval_run"] = {k: fin.get(k) for k in ("id", "status", "trials", "completed", "successes")}
        report["event_counts"] = {}
        for e in events():
            if e["type"].startswith(("dispatch.reset", "dispatch.resume", "eval.", "task.", "payment.")):
                report["event_counts"][e["type"]] = report["event_counts"].get(e["type"], 0) + 1
        report["handler_errors"] = {h.name: h.last_error for h in reg.handlers if h.errors}
        checks = {
            "eval finished": fin["status"] == "done" and int(fin["completed"]) == TRIALS,
            "a reset task per trial": len(report["resets"]) == TRIALS,
            "every reset completed": all(r["status"] == "completed" for r in report["resets"]),
            "every reset has a $2 payment row": all(r["payment"]["amount_cents"] == 200 and r["payment"]["status"] in
                                                     ("pending", "paid", "awaiting_onboarding") for r in report["resets"]),
            "resume_policy for every reset": all(r.get("resume_policy") for r in report["resets"]),
            "next trial < 3 s after Done": all(r["next_trial"]["started_after_done_s"] < 3.0 for r in report["resets"] if r["next_trial"]),
            "nobody-claimed reset escalated": any(r.get("escalation") for r in report["resets"]) or a.web >= TRIALS,
            "scripted resets attached a robot-camera photo": all(r["photo"]["status"] == 200 for r in report["resets"] if r["by"] == "script"),
            "no handler errors": not report["handler_errors"],
        }
        report["checks"] = checks
        report["result"] = "PASS" if all(checks.values()) else "FAIL"
        log(report["result"], **checks)
        if a.hold:
            log(f"holding the stack for {a.hold:.0f}s", web=web if a.web else None)
            time.sleep(a.hold)
        return 0 if report["result"] == "PASS" else 1
    except BaseException as e:
        report["result"] = f"FAIL: {type(e).__name__}: {e}"
        log("FAIL", error=repr(e)[:500], logs=str(st.logs))
        raise
    finally:
        (OUT / "proof.json").write_text(json.dumps(report, indent=2, default=str))
        if bus is not None:
            try:
                evl.get_evaluator(bus).stop()
                asyncio.run_coroutine_threadsafe(bus.stop(), loop).result(10)
            except Exception:  # noqa: BLE001
                pass
        for p in procs:
            try:
                os.killpg(p.pid, 15)
            except ProcessLookupError:
                pass
        st.stop()


if __name__ == "__main__":
    sys.exit(main())
