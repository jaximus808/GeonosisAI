"""TDM-125 proof (LOCAL ONLY, [FAKE] data): a split-robot ticket. Baseline and evals run on robot A
(a fake SIM robot, standing in for sim-yam-mujoco); the paid clean-teleop demos are claimed and
driven on robot B (a fake real-mode robot standing in for yam-01: the fake adapter, no hardware).

    cd services/orchestrator && RDF_LLM_BACKEND=claude_code \
        uv run python ../../spikes/tdm-125/prove_split_robot.py

Boots tests/integration/harness.py on its own ports (SpacetimeDB :3125, orchestrator :8125) with the
working-tree module (create_task payload.robot_id, episode robot from its task, hyperparams
collect_robot_id). Job drops and texts are dry runs with blank Discord / Inkbox credentials.
Never touches maincloud, the live laptop stack, the Jetson or the real arm.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "integration"))

from harness import LocalStack, wait_for  # noqa: E402
from stub_modal import StubTrainer  # noqa: E402
from tdm26_fake_contributor import FakeContributor  # noqa: E402

PORT = int(os.environ.get("TDM125_PORT", "3125"))
ORCH_PORT = int(os.environ.get("TDM125_ORCH_PORT", "8125"))
FAKE_ARGS = ("--time-scale", "400", "--seed", "125", "--estop-clear-seconds", "1", "--heartbeat-seconds", "2",
             "--success-prob", "0.3", "--candidate-success-prob", "0.95", "--candidate-success-schedule", "0,1")
LOOP_TIMEOUT_S = float(os.environ.get("TDM125_TIMEOUT_S", "900"))


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
    st = LocalStack(port=PORT, db="tdm125split")
    drop_dir = tempfile.mkdtemp(prefix="tdm125-drops-")
    trainer = contributor = None
    try:
        st.boot(fake_robot_args=FAKE_ARGS)  # fake-yam-1: real mode (robot B, the yam-01 stand-in)
        robot_b = st.robot("fake-yam-1")
        st.spawn_fake_robot(*FAKE_ARGS, "--name", "fake-sim-yam", "--sim-mode", name="fake-sim-robot")
        robot_a = st.robot("fake-sim-yam")
        st.spawn_orchestrator(port=ORCH_PORT, extra_env={
            "RDF_MODAL_STUB": "1", "FLYWHEEL_AUTO_APPROVE_MAX_CENTS": "5000",
            "RDF_DROP_MODE": "dry_run", "RDF_DROP_DIR": drop_dir, "RDF_TEXT_MODE": "dry_run",
            "DISCORD_BOT_TOKEN": "", "RDF_DROP_DISCORD_WEBHOOK": "", "INKBOX_API_KEY": ""})
        trainer = StubTrainer(st.client(), mode="stub-call").start()
        A, B = int(robot_a["id"]), int(robot_b["id"])
        print(f"robot A (eval, sim) = {A}; robot B (collect, real-mode fake standing in for yam-01) = {B}")

        # 1. Create: evals on A, collect on B.
        parsed = {"task_description": "[FAKE] Put the cube on the plate (TDM-125 split-robot proof).",
                  "target_success": 0.8, "budget_usd": 60, "policy_prompt": "put the cube on the plate",
                  "eval_trials": None}
        code, out = http("POST", "/tickets", {"prompt": "cube on plate", "robot_mode": "sim", "robot_id": A,
                                              "collect_robot_id": B, "parsed": parsed,
                                              "settings_overrides": {"eval_trials": 10, "target_episodes": 3,
                                                                     "max_iterations": 3}})
        check(code == 201 and out["robot"]["id"] == A and (out.get("collect_robot") or {}).get("id") == B,
              f"ticket created: evals on A={A}, collect on B={B} ({code})")
        tid = int(out["ticket_id"])
        hp = json.loads(st.sql(f"SELECT * FROM run_settings WHERE ticket_id = {tid}")[0]["hyperparams_json"])
        check(hp.get("collect_robot_id") == B, f"run_settings hyperparams.collect_robot_id = {B}")
        ev = [json.loads(e["payload_json"]) for e in st.events() if e["type"] == "ticket.collect_robot_set"]
        check(any(p.get("ticket_id") == tid and p.get("collect_robot_id") == B for p in ev), "ticket.collect_robot_set logged")
        code, bad = http("PATCH", f"/tickets/{tid}/settings", {"hyperparams": {"collect_robot_id": 999}})
        check(code == 422, "Settings PATCH to an unknown collect robot is a 422")
        contributor = FakeContributor(st.host, st.db, ticket_id=tid).start()

        # 2. Baseline on A.
        base = wait_for(lambda: [r for r in st.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}")
                                 if r["kind"] == "baseline" and r["status"] == "done"], 240, "baseline done", every=0.5)
        check(all(r["robot_id"] == A for r in base), f"baseline eval {base[0]['id']} ran on robot A ({base[0]['successes']}/{base[0]['trials']})")

        # 3. The loop runs to the end.
        wait_for(lambda: st.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]["status"] in ("shipped", "failed"),
                 LOOP_TIMEOUT_S, f"ticket {tid} finished (orchestrator log {st.logs / 'orchestrator.log'})", every=2)
        t = st.sql(f"SELECT * FROM ticket WHERE id = {tid}")[0]
        print(f"ticket {tid} ended {t['status']} at iteration {t['iteration']}")
        plans = st.sql(f"SELECT * FROM data_plan WHERE ticket_id = {tid}")
        check(bool(plans) and all(p["data_type"] == "clean_teleop" for p in plans),
              f"planner chose clean_teleop for every plan ({[p['data_type'] for p in plans]})")
        ev_plans = [(json.loads(e["payload_json"]).get("evidence") or {}) for e in st.events()
                    if e["type"] == "planner.data_plan" and json.loads(e["payload_json"]).get("ticket_id") == tid]
        check(bool(ev_plans) and all(((e.get("inputs") or {}).get("collect_robot") or {}).get("clean_only") for e in ev_plans),
              "every plan's evidence carries the collect robot's capabilities (clean_only)")
        tasks = st.sql(f"SELECT * FROM task WHERE ticket_id = {tid}")
        data_tasks = [x for x in tasks if x["type"] in ("clean_teleop", "recovery_takeover")]
        check(bool(data_tasks) and all(x["robot_id"] == B for x in data_tasks),
              f"{len(data_tasks)} collection task(s), all on robot B")
        claimed = [x for x in data_tasks if x["assignee_id"]]
        check(bool(claimed), f"{len(claimed)} collection task(s) claimed")
        cmds = st.sql("SELECT * FROM robot_command")
        tele = [c for c in cmds if c["type"] == "start_teleop" and c["ticket_id"] == tid]
        check(bool(tele) and all(c["robot_id"] == B for c in tele), f"{len(tele)} start_teleop command(s), all to robot B")
        evals = st.sql(f"SELECT * FROM eval_run WHERE ticket_id = {tid}")
        check(all(r["robot_id"] == A for r in evals), f"all {len(evals)} eval run(s) on robot A")
        check(any(r["kind"] == "candidate" for r in evals), "a candidate eval ran (back on robot A)")
        eps = st.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}")
        data_eps = [e for e in eps if e["kind"] in ("clean_teleop", "recovery_takeover")]
        acc = [e for e in data_eps if e["review_status"] == "accepted"]
        check(bool(acc) and all(e["robot_id"] == B for e in data_eps),
              f"{len(data_eps)} collection episode(s) labelled robot B ({len(acc)} accepted)")
        trains = st.sql(f"SELECT * FROM training_run WHERE ticket_id = {tid}")
        used = {int(i) for r in trains for i in (r.get("episode_ids") or [])}
        check(bool(used) and used <= {e["id"] for e in acc}, f"training used {len(used)} real-arm (robot B) episode(s)")

        # 4. Report.
        code, rep = http("GET", f"/tickets/{tid}/report")
        sp = (rep.get("robot_mode") or {}).get("split") or {}
        check(code == 200 and sp.get("collect_robot_id") == B and sp.get("collect_mode") == "real"
              and sp.get("eval_mode") == "sim", f"report split: {sp.get('line')}")
        check("not real-arm success rates" in sp.get("note", ""), "report says the eval numbers are sim, not real-arm")
        print(json.dumps({"ticket_id": tid, "robot_a": A, "robot_b": B, "line": sp.get("line")}))
        print("TDM-125 proof OK")
    finally:
        for x in (trainer, contributor):
            if x is not None:
                x.stop()
        st.stop()


if __name__ == "__main__":
    main()
