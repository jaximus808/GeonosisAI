"""TDM-107 proof (LOCAL ONLY, [FAKE] data): a ticket created in Sim mode runs its baseline on the sim
robot, is switched to Real, and from then on its tasks and evals run on the real robot row; the
report keeps the two apart.

    cd services/orchestrator && RDF_LLM_BACKEND=claude_code \
        uv run python ../../spikes/tdm-107-robot-mode/prove_robot_mode.py

What it boots (tests/integration/harness.py, its own port so it never meets another worker's stack):
  * a throwaway SpacetimeDB on 127.0.0.1:$TDM107_PORT (default 3107) with the working-tree module
    (so the new ``set_ticket_robot`` reducer is there; maincloud is never touched);
  * two fake robots: ``fake-yam-1`` (real mode) and ``fake-sim-yam`` (``--sim-mode``: registered with
    ``camera_config_json.sim = true``, standing in for the MuJoCo sim robot);
  * the real orchestrator (stub Modal, auto-approve, a random RDF_OPERATOR_KEY) plus the TDM-26 fake
    contributor and the stub trainer.
Diagnose / planner make LLM calls: run it with RDF_LLM_BACKEND=claude_code (or droid) to spend no API
credits. Everything written is [FAKE]-tagged by the harness.

Exit 0 and a summary line per check when the done condition holds.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack, wait_for  # noqa: E402
from stub_modal import StubTrainer  # noqa: E402
from tdm26_fake_contributor import FakeContributor  # noqa: E402

PORT = int(os.environ.get("TDM107_PORT", "3107"))
ORCH_PORT = int(os.environ.get("TDM107_ORCH_PORT", "8107"))
FAKE_ARGS = ("--time-scale", "400", "--seed", "107", "--estop-clear-seconds", "1", "--heartbeat-seconds", "2",
             "--success-prob", "0.3", "--candidate-success-prob", "0.95", "--candidate-success-schedule", "0,1")
LOOP_TIMEOUT_S = float(os.environ.get("TDM107_TIMEOUT_S", "600"))


def http(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{ORCH_PORT}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        raise SystemExit(1)


def main() -> None:
    st = LocalStack(port=PORT, db="tdm107mode")
    key = secrets.token_hex(24)  # this run's operator key; never printed
    trainer = contributor = None
    try:
        st.boot(fake_robot_args=FAKE_ARGS)
        real = st.robot("fake-yam-1")
        st.spawn_fake_robot(*FAKE_ARGS, "--name", "fake-sim-yam", "--sim-mode", name="fake-sim-robot")
        sim = st.robot("fake-sim-yam")
        st.spawn_orchestrator(port=ORCH_PORT, extra_env={"RDF_MODAL_STUB": "1", "FLYWHEEL_AUTO_APPROVE_MAX_CENTS": "5000",
                                                         "RDF_OPERATOR_KEY": key})
        trainer = StubTrainer(st.client(), mode="stub-call").start()

        # 1. Create in Sim mode (the parsed reading is sent, so no LLM call here).
        parsed = {"task_description": "[FAKE] Put the cube on the plate (TDM-107 robot mode proof).",
                  "target_success": 0.8, "budget_usd": 50, "policy_prompt": "put the cube on the plate",
                  "eval_trials": None}
        code, out = http("POST", "/tickets", {"prompt": "cube on plate", "robot_mode": "sim", "parsed": parsed,
                                              "settings_overrides": {"eval_trials": 10, "target_episodes": 3,
                                                                     "max_iterations": 3}})
        check(code == 201 and out["robot_mode"] == "sim" and out["robot"]["id"] == sim["id"],
              f"ticket created in sim mode on robot {sim['id']} ({code})")
        tid = int(out["ticket_id"])
        contributor = FakeContributor(st.host, st.db, ticket_id=tid).start()

        # 2. Baseline runs on the sim robot.
        base = wait_for(lambda: [r for r in st.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}")
                                 if r["kind"] == "baseline" and r["status"] == "done"], 180, "sim baseline done", every=0.5)
        check(all(r["robot_id"] == sim["id"] for r in base), f"baseline eval {base[0]['id']} ran on the sim robot")

        # 3. Operator switches to Real. Without the key: refused.
        code, _ = http("POST", f"/tickets/{tid}/robot_mode", {"mode": "real"})
        check(code == 401, "switch without the operator key is refused (401)")
        code, sw = wait_for(lambda: (lambda c: c if c[0] != 409 else None)(
            http("POST", f"/tickets/{tid}/robot_mode", {"mode": "real", "reason": "the YAM arrived"},
                 {"X-RDF-Operator-Key": key, "X-Human-Id": "tdm107-proof"})), 60, "switch accepted", every=1)
        check(code == 200 and sw["changed"] and sw["robot_mode"] == "real" and sw["robot"]["id"] == real["id"],
              f"switched to the real robot {real['id']} (ticket in {sw['status']})")
        switched_at = max(e["ts"] for e in st.events() if e["type"] == "ticket.robot_mode_changed")
        ev = [json.loads(e["payload_json"]) for e in st.events() if e["type"] == "ticket.robot_mode_changed"]
        check(ev and ev[-1]["from_mode"] == "sim" and ev[-1]["to_mode"] == "real" and ev[-1]["by"] == "human:tdm107-proof",
              "ticket.robot_mode_changed logged sim -> real by the operator")

        # 4. The loop runs to the end on the real robot.
        wait_for(lambda: st.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]["status"] in ("shipped", "failed"),
                 LOOP_TIMEOUT_S, f"ticket {tid} finished (orchestrator log {st.logs / 'orchestrator.log'})", every=2)
        t = st.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]
        print(f"ticket {tid} ended {t['status']} at iteration {t['iteration']}")
        tasks = st.sql(f"SELECT * FROM task WHERE ticket_id = {tid}")
        new_tasks = [x for x in tasks if x["created_at"] > switched_at]
        check(bool(new_tasks) and all(x["robot_id"] == real["id"] for x in new_tasks),
              f"{len(new_tasks)} task(s) created after the switch, all on the real robot")
        evals = st.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}")
        cands = [r for r in evals if r["kind"] == "candidate"]
        check(bool(cands) and all(r["robot_id"] == real["id"] for r in cands),
              f"{len(cands)} candidate eval(s), all on the real robot")
        eps = [e for e in st.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}") if e["created_at"] > switched_at]
        check(all(e["robot_id"] == real["id"] for e in eps), f"{len(eps)} episode(s) after the switch, all on the real robot")
        trains = st.sql(f"SELECT * FROM training_run WHERE ticket_id = {tid}")
        sim_eps = {e["id"] for e in st.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}") if e["robot_id"] == sim["id"]}
        used = {int(i) for r in trains for i in (r.get("episode_ids") or [])}
        check(not (used & sim_eps), f"training used {len(used)} episode(s), none from the sim robot")

        # 5. The report keeps sim and real apart.
        code, rep = http("GET", f"/tickets/{tid}/report")
        rm = rep["robot_mode"]
        check(code == 200 and rm["current"] == "real", "report: current mode real")
        check(rm["by_mode"]["sim"]["baseline"] is not None and rm["by_mode"]["sim"]["baseline"]["robot_mode"] == "sim",
              f"report: sim baseline listed under by_mode.sim ({rm['by_mode']['sim']['successes']}/{rm['by_mode']['sim']['trials']})")
        check(rep["baseline"] is None or rep["baseline"]["robot_mode"] == "real",
              "report: the before number is not the sim baseline")
        check(rep["final"] is None or rep["final"]["robot_mode"] == "real", "report: the after number is a real-robot eval")
        check(rm["by_mode"]["real"]["trials"] >= 10 and all(v["robot_mode"] == "real" for v in rm["by_mode"]["real"]["eval_runs"]),
              f"report: real results {rm['by_mode']['real']['successes']}/{rm['by_mode']['real']['trials']} (real evals only)")
        print("TDM-107 proof OK")
    finally:
        for x in (trainer, contributor):
            if x is not None:
                x.stop()
        st.stop()


if __name__ == "__main__":
    main()
