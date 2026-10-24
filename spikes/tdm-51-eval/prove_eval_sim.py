"""TDM-51 end-to-end SIM proof: the robot-side eval trial runner against the TDM-67 orchestrator side.

Everything is local and throwaway, every outcome is SIM (SimArm + synthetic cameras + the TDM-50
fake policy server), and the operator's verdicts are scripted ``trial_outcome`` commands:

  * local SpacetimeDB (TDM-28 harness) on a free port, db ``tdm51eval``, data under $TMPDIR/tdm-51/
  * the real orchestrator media API (``rdf_orchestrator.media_api``) signing for a local stand-in
    of the Modal media store, so episodes really upload and ``create_episode`` really runs
  * the real robot client: ``rdf-robot-client --sim --sim-policy --cameras synthetic``
  * in this process: an orchestrator bus with ONLY the TDM-67 eval handlers + the TDM-26
    dispatcher (EVAL_SOLO_MODE=1 resets), no flywheel, so no diagnose/planner Claude calls.

Scenario A, a 3-trial baseline eval: trial 0 operator PASS during the rollout (ends it early),
trial 1 operator FAIL with a note after the rollout, trial 2 no answer (recorded as a failure,
"outcome not confirmed"). Expect 3 eval_trial rows with episodes, a reset between trials, and the
eval_run finalized by the orchestrator (1/3 + Wilson CI).

Scenario B, e-stop mid-trial: a second eval, soft_estop during trial 1. Expect no row for trial 1,
run_eval left ``preempted``, the eval_run aborted ``cancelled`` (estop), the robot in estop, and
nothing restarting on its own.

    cd services/orchestrator && uv run python ../../spikes/tdm-51-eval/prove_eval_sim.py

Never touches maincloud or the real Modal Volume.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
OUT = REPO / "spikes" / "tdm-51-eval" / "tmp"
SIGNING_KEY = "tdm51-proof-signing-key-0123456789"
ROBOT = "test.tdm51-sim"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def log(msg: str, **kw) -> None:
    print(f"[tdm51-proof] {msg}" + (f" {json.dumps(kw, default=str)}" if kw else ""), file=sys.stderr, flush=True)


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
    """Local stand-in for the Modal media store: PUT/GET /o/{key}?exp&sig, same HMAC as media_api."""

    def __init__(self, root: Path) -> None:
        self.dir = root
        self.dir.mkdir(parents=True, exist_ok=True)
        store = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self, method):
                u = urlsplit(self.path)
                q = parse_qs(u.query)
                k = unquote(u.path[len("/o/"):])
                want = hmac.new(SIGNING_KEY.encode(), f"{method}\n{k}\n{int(q['exp'][0])}".encode(),
                                hashlib.sha256).hexdigest()
                return k if hmac.compare_digest(want, q["sig"][0]) else None

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
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


def main() -> int:
    from harness import LocalStack  # TDM-28
    from rdf_common.spacetime_client import SpacetimeClient

    root = Path(tempfile.gettempdir()) / "tdm-51"
    OUT.mkdir(parents=True, exist_ok=True)
    st = LocalStack(port=free_port(), db="tdm51eval", root=root)
    procs: list[subprocess.Popen] = []
    loop = asyncio.new_event_loop()
    bus = None
    report: dict = {"label": "SIM: SimArm + synthetic cameras + fake policy server; scripted operator verdicts",
                    "scenarios": {}}
    try:
        st.start_server()
        st.publish()  # the working-tree module (includes the TDM-51 'trial_outcome' command type)
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        orch = SpacetimeClient(st.host, st.db, st.token("orchestrator"))
        run_dir = st.root

        # ---- media: local store + the REAL orchestrator media API signing for it
        store = LocalMediaStore(run_dir / "media-store")
        media_port = free_port()
        media_env = {**os.environ, "RDF_MEDIA_BACKEND": "modal", "RDF_MEDIA_STORE_URL": store.url,
                     "RDF_MEDIA_SIGNING_KEY": SIGNING_KEY, "RDF_MEDIA_BUCKET": "rdf-media"}
        procs.append(subprocess.Popen(
            ["uv", "run", "--quiet", "uvicorn", "rdf_orchestrator.media_api:app", "--host", "127.0.0.1",
             "--port", str(media_port)], cwd=REPO / "services" / "orchestrator", env=media_env,
            stdout=open(st.logs / "media-api.log", "wb"), stderr=subprocess.STDOUT, start_new_session=True))
        media_url = f"http://127.0.0.1:{media_port}"
        import httpx

        wait_for(lambda: httpx.get(f"{media_url}/media/healthz", timeout=1).status_code == 200, 60, "media API")

        # ---- the TDM-50 fake policy server ([FAKE] ramp chunks, ~120 ms latency)
        pol_port = free_port()
        procs.append(subprocess.Popen(
            ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "python", "-c",
             "import time; from rdf_robot_client.fake_policy_server import FakePolicyServer, jitter_schedule; "
             f"FakePolicyServer(schedule=jitter_schedule(120, 20), port={pol_port}).start(); time.sleep(1e9)"],
            stdout=open(st.logs / "fake-policy.log", "wb"), stderr=subprocess.STDOUT, start_new_session=True))
        wait_for(lambda: httpx.get(f"http://127.0.0.1:{pol_port}/health", timeout=1).status_code == 200, 60,
                 "fake policy server")

        # ---- the real robot client on the sim arm with the TDM-51 trial runner
        robot_env = {**os.environ, "ORCHESTRATOR_URL": media_url, "POLICY_BACKEND": "spike",
                     "POLICY_ENDPOINT_URL": f"http://127.0.0.1:{pol_port}", "RDF_EPISODES_DIR": str(run_dir / "episodes"),
                     "RDF_EVAL_OUTCOME_TIMEOUT_S": "6", "RDF_EVAL_UPLOAD_WAIT_S": "60"}
        procs.append(subprocess.Popen(
            ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "rdf-robot-client",
             "--sim", "--sim-policy", "--cameras", "synthetic", "--name", ROBOT, "--host", st.host, "--db", st.db,
             "--token-file", str(st.tokens / "robot_client.token"), "--heartbeat-s", "1"],
            env=robot_env, stdin=subprocess.DEVNULL, stdout=open(st.logs / "robot-client.log", "wb"),
            stderr=subprocess.STDOUT, start_new_session=True))
        robot = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM robot WHERE name = '{ROBOT}'")
                                       if r["status"] == "online"), None), 90, "sim robot online")
        rid = int(robot["id"])
        log("robot online", robot_id=rid)

        # ---- orchestrator side: ONLY the TDM-67 eval handlers + TDM-26 dispatcher, on the local DB
        os.environ.update({"EVAL_SOLO_MODE": "1", "EVAL_SOLO_RESET_DELAY_S": "2", "EVAL_TRIAL_TIMEOUT_S": "8",
                           "EVAL_WATCHDOG_S": "2", "EVAL_ROBOT_STALE_S": "30", "POLICY_SERVER_URL": "",
                           "SPACETIME_HOST": st.host, "SPACETIME_DB_NAME": st.db,
                           "SPACETIME_TOKEN": st.token("orchestrator")})
        from rdf_orchestrator import dispatcher as dsp
        from rdf_orchestrator import evaluate as evl
        from rdf_orchestrator.bus import Bus, Registry
        from rdf_orchestrator.handlers import dispatcher as h_dsp
        from rdf_orchestrator.handlers import evaluate as h_evl
        from rdf_orchestrator.stdb import SpacetimeClient as BusStdb

        writer = lambda: SpacetimeClient(st.host, st.db, st.token("orchestrator"))  # noqa: E731
        evl.make_writer = writer
        dsp.make_writer = writer
        reg = Registry()
        h_dsp.register(reg)
        h_evl.register(reg, None)  # no flywheel: this script plays the flywheel's EvalStep
        bus = Bus(reg, BusStdb(host=st.host, db=st.db, token=st.token("orchestrator"), max_backoff=2.0), workers=4)
        threading.Thread(target=loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(bus.start(), loop).result(15)
        wait_for(lambda: bus.state == "live" and bus.get("robot", rid), 30, "orchestrator bus live")
        ev = evl.get_evaluator(bus)

        def events(after: int = 0) -> list[dict]:
            return sorted(orch.sql(f"SELECT * FROM event_log WHERE id > {after}"), key=lambda r: r["id"])

        def robot_event(type_: str, run_id: int, index: int, after: int):
            for e in events(after):
                if e["type"] == type_:
                    p = json.loads(e["payload_json"])
                    if p.get("eval_run_id") == run_id and p.get("index") == index:
                        return e
            return None

        def verdict(tid: int, run_id: int, index: int, success: bool, note: str) -> int:
            orch.call_reducer("issue_robot_command", rid, "trial_outcome",
                              json.dumps({"success": success, "note": note, "eval_run_id": run_id, "index": index,
                                          "by": "[SIM] scripted operator"}), tid)
            cmd = max(orch.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid} AND type = 'trial_outcome'"),
                      key=lambda c: int(c["id"]))
            done = wait_for(lambda: next((c for c in orch.sql(f"SELECT * FROM robot_command WHERE id = {cmd['id']}")
                                          if c["status"] in ("done", "failed")), None), 15, "trial_outcome handled")
            log("operator verdict sent", index=index, success=success, status=done["status"],
                result=done.get("result_json"))
            return int(cmd["id"])

        def new_ticket(desc: str) -> int:
            before = {int(r["id"]) for r in orch.sql("SELECT * FROM ticket")}
            orch.call_reducer("create_ticket", rid, desc, 0.8, 5000,
                              json.dumps({"eval_trials": 3, "hyperparams": {"prompt": "pick up the cube"}}))
            tid = wait_for(lambda: next((int(r["id"]) for r in orch.sql("SELECT * FROM ticket")
                                         if int(r["id"]) not in before), None), 10, "ticket")
            orch.call_reducer("set_ticket_status", tid, "baseline", "test.tdm51 proof (stands in for the flywheel)")
            wait_for(lambda: (bus.get("ticket", tid) or {}).get("status") == "baseline", 10, "ticket in mirror")
            return tid

        # =========================== scenario A: 3-trial baseline eval with scripted operator verdicts
        ev0 = max((int(e["id"]) for e in events()), default=0)
        tid = new_ticket("test.tdm51: pick up the cube [SIM]")
        out = ev.ensure_eval(tid, why="tdm51 proof scenario A")
        log("ensure_eval", outcome=out)
        run = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}")), None),
                       15, "eval_run")
        run_id = int(run["id"])
        # trial 0: PASS ~2 s into the rollout
        wait_for(lambda: robot_event("eval.trial_started", run_id, 0, ev0), 60, "trial 0 started")
        time.sleep(2.0)
        verdict(tid, run_id, 0, True, "[SIM] scripted operator: cube in the bowl")
        # trial 1: the rollout runs to its 8 s limit, then FAIL with a note
        wait_for(lambda: robot_event("eval.trial_awaiting_outcome", run_id, 1, ev0), 90, "trial 1 asks for outcome")
        verdict(tid, run_id, 1, False, "[SIM] scripted operator: cube slipped out of the gripper")
        # a late / wrong verdict must be refused (trial 1 is closed, the open trial is 2 or none)
        late = verdict(tid, run_id, 1, True, "late click for trial 1")
        # trial 2: no answer -> failure "outcome not confirmed" after RDF_EVAL_OUTCOME_TIMEOUT_S
        fin = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM eval_run WHERE id = {run_id}")
                                     if r["status"] in ("done", "failed", "cancelled")), None), 150, "eval_run finished")
        trials = sorted(orch.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {run_id}"), key=lambda r: r["trial_index"])
        eps = {int(e["id"]): e for e in orch.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}")}
        cmds = sorted(orch.sql(f"SELECT * FROM robot_command WHERE ticket_id = {tid}"), key=lambda c: int(c["id"]))
        resets = [t for t in orch.sql(f"SELECT * FROM task WHERE ticket_id = {tid}") if t["type"] == "reset"]
        late_row = orch.sql(f"SELECT * FROM robot_command WHERE id = {late}")[0]
        a = {
            "eval_run": {k: fin.get(k) for k in ("id", "kind", "status", "trials", "completed", "successes", "ci_low",
                                                  "ci_high", "checkpoint_uri", "summary")},
            "trials": [{k: t.get(k) for k in ("trial_index", "success", "duration_ms", "episode_id", "failure_note")}
                       for t in trials],
            "episodes": [{"id": i, "kind": e["kind"], "success": e.get("success"), "frames": e.get("frame_count"),
                          "storage_uri": e["storage_uri"]} for i, e in sorted(eps.items())],
            "commands": [(int(c["id"]), c["type"], c["status"]) for c in cmds],
            "reset_tasks": [(int(t["id"]), t["status"]) for t in resets],
            "late_verdict_refused": late_row["status"] == "failed",
            "late_verdict_error": late_row.get("result_json"),
        }
        report["scenarios"]["A_three_trials"] = a
        log("scenario A", **a)
        assert fin["status"] == "done", fin
        assert [t["trial_index"] for t in trials] == [0, 1, 2]
        assert [t["success"] for t in trials] == [True, False, False]
        assert "outcome not confirmed" in (trials[2]["failure_note"] or "")
        assert all(int(t["episode_id"]) in eps for t in trials), "every trial has an uploaded episode"
        assert int(fin["successes"]) == 1 and late_row["status"] == "failed"
        run_eval_cmd = next(c for c in cmds if c["type"] == "run_eval")
        assert run_eval_cmd["status"] == "done"
        n_resume = sum(1 for c in cmds if c["type"] == "resume_policy")
        assert n_resume >= 2, cmds

        # =========================== scenario B: soft e-stop mid-trial
        ev1 = max(int(e["id"]) for e in events())
        # the dispatcher may send a reset_request job after the last trial (run_eval was already done):
        # wait until the robot is free BEFORE the ticket exists, or kick_robot starts B by itself
        wait_for(lambda: not ev.open_jobs(rid), 60, "robot job lock free")
        tid2 = new_ticket("test.tdm51: e-stop mid-trial [SIM]")
        log("ensure_eval B", outcome=ev.ensure_eval(tid2, why="tdm51 proof scenario B"))  # dispatched | in_progress
        run2 = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid2}")), None),
                        15, "eval_run B")
        run2_id = int(run2["id"])
        wait_for(lambda: robot_event("eval.trial_started", run2_id, 0, ev1), 60, "B trial 0 started")
        time.sleep(1.5)
        verdict(tid2, run2_id, 0, True, "[SIM] scripted operator: pass")
        wait_for(lambda: robot_event("eval.trial_started", run2_id, 1, ev1), 90, "B trial 1 started")
        time.sleep(2.0)
        orch.call_reducer("issue_robot_command", rid, "soft_estop", json.dumps({"reason": "[SIM] proof e-stop"}), tid2)
        log("soft_estop issued mid trial 1")
        fin2 = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM eval_run WHERE id = {run2_id}")
                                      if r["status"] != "running"), None), 30, "eval B aborted")
        time.sleep(8.0)  # nothing may restart on its own
        trials2 = sorted(orch.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {run2_id}"), key=lambda r: r["trial_index"])
        cmds2 = sorted(orch.sql(f"SELECT * FROM robot_command WHERE ticket_id = {tid2}"), key=lambda c: int(c["id"]))
        rob = orch.sql(f"SELECT * FROM robot WHERE id = {rid}")[0]
        abort_ev = [json.loads(e["payload_json"]) for e in events(ev1) if e["type"] == "eval.aborted"]
        interrupted = [json.loads(e["payload_json"]) for e in events(ev1) if e["type"] == "eval.trial_interrupted"]
        b = {
            "eval_run": {k: fin2.get(k) for k in ("id", "status", "trials", "completed", "successes")},
            "trials": [{k: t.get(k) for k in ("trial_index", "success", "failure_note")} for t in trials2],
            "commands": [(int(c["id"]), c["type"], c["status"]) for c in cmds2],
            "robot_status_after_8s": rob["status"],
            "abort_event": abort_ev[:1],
            "robot_trial_interrupted_event": interrupted[:1],
            "ensure_eval_after": ev.ensure_eval(tid2, why="tdm51 proof: must wait for a human"),
        }
        report["scenarios"]["B_estop_mid_trial"] = b
        log("scenario B", **b)
        assert fin2["status"] == "cancelled", fin2
        assert [t["trial_index"] for t in trials2] == [0], trials2
        assert next(c for c in cmds2 if c["type"] == "run_eval")["status"] == "preempted"
        assert sum(1 for c in cmds2 if c["type"] == "run_eval") == 1, "nothing restarted"
        assert rob["status"] == "estop"
        report["result"] = "PASS"
        log("PASS")
        return 0
    except BaseException as e:
        report["result"] = f"FAIL: {type(e).__name__}: {e}"
        log("FAIL", error=repr(e)[:500], logs=str(st.logs))
        raise
    finally:
        (OUT / "proof.json").write_text(json.dumps(report, indent=2, default=str))
        for name in ("robot-client.log", "media-api.log", "fake-policy.log"):
            src = st.logs / name
            if src.exists():
                shutil.copy2(src, OUT / name)
        if bus is not None:
            try:
                asyncio.run_coroutine_threadsafe(bus.stop(), loop).result(5)
            except Exception:  # noqa: BLE001
                pass
        for p in procs:
            try:
                os.killpg(p.pid, 15)
            except ProcessLookupError:
                pass
        for p in procs:
            try:
                p.wait(8)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, 9)
        st.stop()


if __name__ == "__main__":
    raise SystemExit(main())
