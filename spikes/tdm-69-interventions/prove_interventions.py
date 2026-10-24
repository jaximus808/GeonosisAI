"""TDM-69 proof: DAgger-style interventions end to end on a THROWAWAY local SpacetimeDB. Everything is [FAKE].

    cd services/orchestrator && uv run python ../../spikes/tdm-69-interventions/prove_interventions.py

What runs (nothing touches maincloud; no Claude call: the ticket is placed in ``dispatch`` with a
[FAKE proof driver] data plan before the orchestrator boots, and the harness review config stubs the
reviewer's Claude judgement):
  * the TDM-28 LocalStack on 127.0.0.1:3069 (db ``tdm69iv``), module published from this checkout
  * ``tests/integration/tdm69_fake_intervention_robot.py``: the TDM-28 fake robot + the robot side of
    interventions (real FailureDetector over synthetic progress), scripted rollouts:
        1 no_progress    -> nobody claims (the fake contributor skips it) -> claim timeout -> reset
        2 timeout        -> takeover -> handback -> recovery episode
        3 success        -> no intervention
        4 slow           -> the operator presses "Failing" (POST /robots/{id}/interventions/flag)
                            -> takeover -> handback -> recovery episode
        5 success_check  -> takeover -> handback -> recovery episode
  * the real orchestrator (all handlers; RDF_MODAL_STUB=1 so training is not launched)
  * TDM-26 FakeContributor doing the paid resets, TDM-69 TakeoverContributor doing the takeovers
Done when the plan's 3 recovery episodes are reviewer-accepted and are in the training_run the
flywheel queues. Evidence: spikes/tdm-69-interventions/tmp/proof-<ts>.json
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack, wait_for  # noqa: E402
from tdm26_fake_contributor import FakeContributor  # noqa: E402
from tdm69_fake_takeover_contributor import TakeoverContributor  # noqa: E402

PORT, DB, ORCH = int(os.environ.get("TDM69_PORT", "3069")), "tdm69iv", int(os.environ.get("TDM69_ORCH_PORT", "8069"))
TAG = "[FAKE proof driver TDM-69]"
SCRIPT = "no_progress,timeout,success,slow,success_check"
AMOUNT = 3


def log(msg: str) -> None:
    print(f"[tdm69-proof {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    st = LocalStack(PORT, DB)
    op_key = secrets.token_hex(16)
    fc = tc = None
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        st._spawn("fake-robot", ["uv", "run", "--quiet", "--project", str(REPO / "services" / "robot-client"), "python",
                                 str(REPO / "tests" / "integration" / "tdm69_fake_intervention_robot.py"),
                                 "--host", st.host, "--db", st.db, "--token-file", str(st.tokens / "robot_client.token"),
                                 "--media-dir", str(st.media), "--seed", "69", "--reset-ms", "1000:2000",
                                 "--script", SCRIPT, "--slow-wall-s", "12"])
        robot = st.robot("fake-yam-iv")
        rid = int(robot["id"])
        c = st.client()
        c.call_reducer("create_ticket", rid, "[FAKE] put the cup in the bin (TDM-69 intervention proof)", 0.8, 5000, "")
        tid = int(st.newest("ticket")["id"])
        for to in ("baseline", "diagnose", "plan_data"):
            c.call_reducer("set_ticket_status", tid, to, f"{TAG} skip to collection (no eval, no Claude)")
        c.call_reducer("record_data_plan", tid, "recovery_takeover", AMOUNT,
                       f"{TAG} NOT a planner decision: fixed recovery_takeover x{AMOUNT} to exercise TDM-69",
                       json.dumps({"fake": True}))
        plan = st.newest("data_plan", f"ticket_id = {tid}")
        c.call_reducer("approve_spend", tid, int(plan["id"]), AMOUNT * 500, "test.tdm69")
        c.call_reducer("set_ticket_status", tid, "dispatch", f"{TAG} collect recovery takeovers")
        log(f"ticket {tid} in dispatch, plan {plan['id']} recovery_takeover x{AMOUNT} approved; robot {rid}")

        st.spawn_orchestrator(ORCH, extra_env={
            "INTERVENTION_CLAIM_TIMEOUT_S": "8", "INTERVENTION_WATCHDOG_S": "1", "INTERVENTION_ROLLOUT_TIMEOUT_S": "120",
            "INTERVENTION_VERDICT_WAIT_S": "15", "RDF_MODAL_STUB": "1", "RDF_OPERATOR_KEY": op_key,
            "EVAL_WATCHDOG_S": "0", "FLYWHEEL_AUTO_APPROVE_MAX_CENTS": "0"})
        fc = FakeContributor(st.host, st.db, name="[FAKE] contributor tdm69 resets", types=("reset",),
                             ticket_id=tid).start()
        tc = TakeoverContributor(st.host, st.db, skip_first=1, drive_s=2.0, ticket_id=tid).start()

        # operator flag during the 4th rollout ("slow")
        flagged: dict = {}

        def flagger() -> None:
            def fourth():
                cmds = sorted((r for r in st.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid}")
                               if r["type"] == "run_baseline"), key=lambda r: r["id"])
                return cmds[3] if len(cmds) >= 4 and cmds[3]["status"] == "acked" else None
            cmd = wait_for(fourth, 400, "4th collection rollout acked", every=0.5)
            time.sleep(3)
            flagged.update(http("POST", f"http://127.0.0.1:{ORCH}/robots/{rid}/interventions/flag",
                                {"note": "[FAKE] operator: it is wandering", "by": "proof-operator"},
                                {"X-RDF-Operator-Key": op_key}), command_id=cmd["id"])
            log(f"operator flag -> {flagged.get('outcome')}")

        threading.Thread(target=flagger, daemon=True).start()

        def trained():
            runs = st.sql(f"SELECT * FROM training_run WHERE ticket_id = {tid}")
            return runs[0] if runs else None
        last = [0.0]

        def progress():
            if time.monotonic() - last[0] > 15:
                last[0] = time.monotonic()
                evs = [e["type"] for e in st.events() if e["type"].startswith("intervention.")]
                log(f"... {len(evs)} intervention events; ticket {st.sql(f'SELECT * FROM ticket WHERE id = {tid}')[0]['status']}")
            return trained()
        run = wait_for(progress, 600, "training_run queued by the flywheel", every=1.0)
        time.sleep(2)

        summary = http("GET", f"http://127.0.0.1:{ORCH}/tickets/{tid}/interventions")
        eps = {e["id"]: e for e in st.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}")}
        rec = [e for e in eps.values() if e["kind"] == "recovery_takeover"]
        train_ids = run.get("episode_ids")
        train_ids = json.loads(train_ids) if isinstance(train_ids, str) else list(train_ids or [])
        events = [{"id": e["id"], "actor": e["actor"], "type": e["type"], "payload": json.loads(e["payload_json"])}
                  for e in st.events() if e["type"].startswith(("intervention.", "robot.fake_", "dispatch.reset",
                                                                "task.", "ticket.status", "review."))]
        out = {"tag": "[FAKE] TDM-69 local proof", "ticket_id": tid, "robot_id": rid, "script": SCRIPT,
               "summary": summary, "operator_flag": flagged,
               "recovery_episodes": [{k: e.get(k) for k in ("id", "task_id", "contributor_id", "review_status",
                                                             "takeover_ts", "handback_ts", "duration_ms", "success")}
                                     for e in rec],
               "reviews": st.sql("SELECT * FROM review"),
               "training_run": {"id": run["id"], "status": run["status"], "episode_ids": train_ids},
               "takeover_contributor": {"seen": tc.seen, "done": tc.done, "errors": tc.errors},
               "reset_contributor": {"done": len(fc.done), "errors": fc.errors}, "events": events}
        dst = Path(__file__).parent / "tmp" / f"proof-{time.strftime('%Y%m%d-%H%M%S')}.json"
        dst.parent.mkdir(exist_ok=True)
        dst.write_text(json.dumps(out, indent=2, default=str))

        accepted = sorted(e["id"] for e in rec if e["review_status"] == "accepted")
        checks = {
            "3 recovery episodes accepted": len(accepted) == AMOUNT,
            "accepted recovery episodes are in training_run.episode_ids": set(accepted) <= set(train_ids),
            "one claim timeout fell back to end-trial + reset": summary["timeouts"].get("claim", 0) >= 1
                and summary["by_outcome"].get("unclaimed_timeout", 0) >= 1,
            "operator flag started an intervention": flagged.get("outcome") == "started",
            "detectors seen": {"no_progress", "timeout", "operator", "success_check"} <= set(summary["by_detector"]),
            "every recovery episode has takeover_ts < handback_ts": all(
                e["takeover_ts"] and e["handback_ts"] and e["takeover_ts"] < e["handback_ts"] for e in rec),
            "no contributor errors": not tc.errors and not fc.errors,
        }
        log(f"interventions: {summary['count']} {summary['by_outcome']} detectors {summary['by_detector']} "
            f"median claim wait {summary['median_claim_wait_s']}s")
        log(f"recovery episodes accepted {accepted}; training_run {run['id']} episode_ids {train_ids}")
        for k, v in checks.items():
            log(f"{'PASS' if v else 'FAIL'}  {k}")
        log(f"evidence: {dst}")
        return 0 if all(checks.values()) else 1
    finally:
        for x in (tc, fc):
            if x is not None:
                x.stop()
        st.stop()


if __name__ == "__main__":
    raise SystemExit(main())
