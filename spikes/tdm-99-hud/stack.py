"""TDM-99: a local SIM baseline eval for checking the robot HUD in a browser.

    cd services/orchestrator && uv run python ../../spikes/tdm-99-hud/stack.py [--trials 10] [--verdict-after 4]

Everything is local, throwaway and SIM (SimArm + synthetic cameras + the TDM-50 fake policy server,
scripted "[SIM] scripted operator" verdicts), like spikes/tdm-51-eval/prove_eval_sim.py:
  * SpacetimeDB 127.0.0.1:3199 (db tdm99hud), data under spikes/tdm-99-hud/tmp/ (gitignored)
  * robot stream + /ws/state on 127.0.0.1:8199 (RDF_STREAM=none)
  * the real orchestrator media API (local store) and, in this process, ONLY the TDM-67 eval handlers
    + TDM-26 dispatcher (EVAL_SOLO_MODE=1), so no Claude calls
Never touches maincloud or the demo ports (3028 / 8028 / 3737).

It writes spikes/tdm-99-hud/tmp/stack.json (robot id, ticket id, view token, state URL, the HUD URL)
and tmp/trials.json (the eval_trial rows, refreshed every 0.5 s, to compare with the HUD).
E-stop on demand: `touch spikes/tdm-99-hud/tmp/estop.trigger`; the issue time lands in tmp/estop.json.
Ctrl-C (SIGTERM) stops everything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))
sys.path.insert(0, str(REPO / "spikes" / "tdm-51-eval"))
OUT = REPO / "spikes" / "tdm-99-hud" / "tmp"
PORT, DB, STREAM_PORT, WEB_PORT = 3199, "tdm99hud", 8199, 3099
ROBOT = "test.tdm99-sim"


def log(msg: str, **kw) -> None:
    print(f"[tdm99] {msg}" + (f" {json.dumps(kw, default=str)}" if kw else ""), file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--verdict-after", type=float, default=4.0, help="s into each trial before the scripted verdict")
    ap.add_argument("--pattern", default="PFPPFPPFPP", help="scripted verdicts per trial (P/F/- = no answer)")
    a = ap.parse_args()

    from harness import LocalStack, wait_for  # TDM-28
    from prove_eval_sim import SIGNING_KEY, LocalMediaStore, free_port  # TDM-51 proof helpers
    from rdf_common.spacetime_client import SpacetimeClient
    from rdf_common.stream_token import load_secret, mint

    OUT.mkdir(parents=True, exist_ok=True)
    for f in ("estop.trigger", "estop.json", "stack.json", "trials.json"):
        (OUT / f).unlink(missing_ok=True)
    st = LocalStack(port=PORT, db=DB, root=OUT / "stack")
    procs: list[subprocess.Popen] = []
    loop = asyncio.new_event_loop()
    bus = None
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        orch = SpacetimeClient(st.host, st.db, st.token("orchestrator"))

        store = LocalMediaStore(st.root / "media-store")
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

        pol_port = free_port()  # [FAKE] ramp chunks, ~120 ms latency
        procs.append(subprocess.Popen(
            ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "python", "-c",
             "import time; from rdf_robot_client.fake_policy_server import FakePolicyServer, jitter_schedule; "
             f"FakePolicyServer(schedule=jitter_schedule(120, 20), port={pol_port}, velocity=(0.25, -0.12, 0.1, 0.15, 0.0, 0.2)).start(); time.sleep(1e9)"],
            stdout=open(st.logs / "fake-policy.log", "wb"), stderr=subprocess.STDOUT, start_new_session=True))
        wait_for(lambda: httpx.get(f"http://127.0.0.1:{pol_port}/health", timeout=1).status_code == 200, 60,
                 "fake policy server")

        robot_env = {**os.environ, "ORCHESTRATOR_URL": media_url, "POLICY_BACKEND": "spike",
                     "POLICY_ENDPOINT_URL": f"http://127.0.0.1:{pol_port}", "RDF_EPISODES_DIR": str(st.root / "episodes"),
                     "RDF_EVAL_OUTCOME_TIMEOUT_S": "6", "RDF_EVAL_UPLOAD_WAIT_S": "60",
                     "RDF_STREAM": "none", "RDF_STREAM_PORT": str(STREAM_PORT)}
        procs.append(subprocess.Popen(
            ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "rdf-robot-client",
             "--sim", "--sim-policy", "--cameras", "synthetic", "--name", ROBOT, "--host", st.host, "--db", st.db,
             "--token-file", str(st.tokens / "robot_client.token"), "--heartbeat-s", "1"],
            env=robot_env, stdin=subprocess.DEVNULL, stdout=open(st.logs / "robot-client.log", "wb"),
            stderr=subprocess.STDOUT, start_new_session=True))
        robot = wait_for(lambda: next((r for r in orch.sql(f"SELECT * FROM robot WHERE name = '{ROBOT}'")
                                       if r["status"] == "online" and "stream" in json.loads(r["camera_config_json"] or "{}")),
                                      None), 120, "sim robot online with its stream")
        rid = int(robot["id"])
        info = json.loads(robot["camera_config_json"])["stream"]
        log("robot online", robot_id=rid, state_url=info.get("state_url"))

        os.environ.update({"EVAL_SOLO_MODE": "1", "EVAL_SOLO_RESET_DELAY_S": "2", "EVAL_TRIAL_TIMEOUT_S": "10",
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
        h_evl.register(reg, None)
        bus = Bus(reg, BusStdb(host=st.host, db=st.db, token=st.token("orchestrator"), max_backoff=2.0), workers=4)
        threading.Thread(target=loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(bus.start(), loop).result(15)
        wait_for(lambda: bus.state == "live" and bus.get("robot", rid), 30, "orchestrator bus live")
        ev = evl.get_evaluator(bus)

        orch.call_reducer("create_ticket", rid, "[SIM] pick up the red cube and put it on the plate", 0.8, 5000,
                          json.dumps({"eval_trials": a.trials, "hyperparams": {"prompt": "pick up the cube"}}))
        tid = wait_for(lambda: max((int(r["id"]) for r in orch.sql("SELECT * FROM ticket")), default=None), 10, "ticket")
        orch.call_reducer("set_ticket_status", tid, "baseline", "test.tdm99 HUD check (stands in for the flywheel)")
        wait_for(lambda: (bus.get("ticket", tid) or {}).get("status") == "baseline", 10, "ticket in mirror")

        tok = mint(load_secret(), robot_id=rid, robot_name=ROBOT, sub="operator:tdm99-view", scope="view", ttl_s=6 * 3600)[0]
        state_url = f"ws://127.0.0.1:{STREAM_PORT}/ws/state"
        hud_url = (f"http://127.0.0.1:{WEB_PORT}/dev/tdm99-hud?robot={rid}&ticket={tid}&url={state_url}"
                   f"#stream_token={tok}&stream_url=ws://127.0.0.1:{STREAM_PORT}/ws/cameras")
        (OUT / "stack.json").write_text(json.dumps({
            "robot_id": rid, "ticket_id": tid, "state_url": state_url, "view_token": tok, "hud_url": hud_url,
            "spacetime_host": st.host, "db": st.db, "web_env": {"NEXT_PUBLIC_SPACETIME_HOST": st.host,
                                                                "NEXT_PUBLIC_SPACETIME_DB_NAME": st.db}}, indent=1))
        log("ensure_eval", outcome=ev.ensure_eval(tid, why="tdm99 HUD check"))
        run = wait_for(lambda: next(iter(orch.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}")), None), 15, "eval_run")
        run_id = int(run["id"])
        log("eval running", eval_run_id=run_id, hud_url=hud_url)

        seen: set[int] = set()
        ev0 = 0
        while not stop.is_set():
            rows = sorted(orch.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {run_id}"), key=lambda r: r["trial_index"])
            er = orch.sql(f"SELECT * FROM eval_run WHERE id = {run_id}")[0]
            (OUT / "trials.json").write_text(json.dumps({
                "t_ms": int(time.time() * 1000), "eval_run": {k: er.get(k) for k in ("id", "status", "trials", "completed", "successes")},
                "trials": [{k: r.get(k) for k in ("trial_index", "success", "failure_note")} for r in rows]}, default=str))
            for e in sorted(orch.sql(f"SELECT * FROM event_log WHERE id > {ev0}"), key=lambda r: r["id"]):
                ev0 = max(ev0, int(e["id"]))
                if e["type"] != "eval.trial_started":
                    continue
                p = json.loads(e["payload_json"])
                i = int(p.get("index", -1))
                if p.get("eval_run_id") != run_id or i in seen:
                    continue
                seen.add(i)
                v = a.pattern[i % len(a.pattern)]
                if v in "PF":
                    def _verdict(i=i, ok=v == "P"):
                        time.sleep(a.verdict_after)
                        orch.call_reducer("issue_robot_command", rid, "trial_outcome", json.dumps(
                            {"success": ok, "note": f"[SIM] scripted operator: {'pass' if ok else 'cube missed the plate'}",
                             "eval_run_id": run_id, "index": i, "by": "[SIM] scripted operator"}), tid)
                        log("verdict", index=i, success=ok)
                    threading.Thread(target=_verdict, daemon=True).start()
            trig = OUT / "estop.trigger"
            if trig.exists():
                trig.unlink()
                t0 = time.time() * 1000
                orch.call_reducer("issue_robot_command", rid, "soft_estop", json.dumps({"reason": "[SIM] tdm99 HUD check"}), tid)
                (OUT / "estop.json").write_text(json.dumps({"issued_ms": t0, "reducer_returned_ms": time.time() * 1000}))
                log("soft_estop issued")
            stop.wait(0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
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
