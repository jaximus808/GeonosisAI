"""TDM-28: the fake robot behaves like the Jetson robot-client on the bus (local SpacetimeDB only)."""
from __future__ import annotations

import json

from conftest import new_ticket
from harness import wait_for


def _issue(st, robot_id, ticket_id, type_, payload=None) -> int:
    st.client().call_reducer("issue_robot_command", robot_id, type_, json.dumps(payload or {}), ticket_id)
    return int(st.newest("robot_command", f"robot_id = {robot_id}")["id"])


def _cmd(st, cid):
    return st.sql(f"SELECT * FROM robot_command WHERE id = {cid}")[0]


def _finished(st, cid, timeout=30):
    return wait_for(lambda: (r := _cmd(st, cid))["status"] in ("done", "failed", "preempted") and r,
                    timeout, f"command {cid} finished")


def _online(st, robot_id, timeout=15):
    wait_for(lambda: st.sql(f"SELECT * FROM robot WHERE id = {robot_id}")[0]["status"] == "online",
             timeout, "robot back online")


def test_run_eval_writes_tagged_fake_trials_episodes_and_resets(stack, robot_id):
    st, c = stack, stack.client()
    tid = new_ticket(st, robot_id, "eval smoke")
    c.call_reducer("set_ticket_status", tid, "baseline", "test.tdm28")
    first_ev = st.events()[-1]["id"]
    c.call_reducer("start_eval_run", tid, "fake://pi05_base", 6)
    run = st.newest("eval_run", f"ticket_id = {tid}")
    cid = _issue(st, robot_id, tid, "run_eval", {"eval_run_id": run["id"]})
    cmd = _finished(st, cid)
    assert cmd["status"] == "done" and cmd["acked_at"] is not None
    result = json.loads(cmd["result_json"])
    assert result["fake"] is True and result["trials"] == 6 and result["eval_run_id"] == run["id"]

    trials = st.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {run['id']}")
    assert sorted(t["trial_index"] for t in trials) == list(range(6))
    assert all(t["failure_note"].startswith("[FAKE]") for t in trials), "every fake trial row is tagged"
    run = st.sql(f"SELECT * FROM eval_run WHERE id = {run['id']}")[0]
    assert run["completed"] == 6 and run["successes"] == sum(t["success"] for t in trials) == result["successes"]

    eps = st.sql(f"SELECT * FROM episode WHERE ticket_id = {tid}")
    assert len(eps) == 6 and {e["id"] for e in eps} == {t["episode_id"] for t in trials}
    for e in eps:
        assert e["kind"] == "baseline" and e["review_status"] == "not_reviewed"
        assert "FAKE-ep-" in e["storage_uri"] and all(c["fake"] for c in json.loads(e["cameras_json"]).values())
        assert e["checkpoint_uri"] == "fake://pi05_base"

    ev = st.events(first_ev)
    types = [e["type"] for e in ev]
    assert types.count("eval.trial") == 6 and "robot.command_acked" in types and "robot.command_done" in types
    resets = [e for e in ev if e["type"] == "robot.reset_requested"]
    assert len(resets) == 5 and all(json.loads(e["payload_json"])["fake"] for e in resets)
    assert types.count("robot.reset_done") == 5

    # The orchestrator-side finalizer accepts the fake trials (numbers match the rows).
    from rdf_orchestrator.stats import EvalRunFinalizer

    fin = EvalRunFinalizer(lambda **kw: c.call_reducer("finalize_eval_run", kw["eval_run_id"], kw["successes"],
                                                       kw["trials"], kw["ci_low"], kw["ci_high"], kw["summary"]))
    for t in trials:
        fin.on_eval_trial(t, run)
    assert st.sql(f"SELECT * FROM eval_run WHERE id = {run['id']}")[0]["status"] == "done"


def test_reset_request_and_simple_commands(stack, robot_id):
    st = stack
    tid = new_ticket(st, robot_id, "reset")
    for type_ in ("reset_request", "home", "load_checkpoint", "stop_teleop", "handback"):
        cmd = _finished(st, _issue(st, robot_id, tid, type_, {"checkpoint_uri": "fake://x"}))
        assert cmd["status"] == "done", (type_, cmd)
        assert json.loads(cmd["result_json"])["fake"] is True
    assert any(e["type"] == "robot.reset_done" and json.loads(e["payload_json"]).get("requested_by")
               for e in st.events())


def test_teleop_produces_pending_dataset_episode(stack, robot_id):
    st = stack
    tid = new_ticket(st, robot_id, "teleop")
    cmd = _finished(st, _issue(st, robot_id, tid, "start_teleop", {"kind": "recovery_takeover"}))
    assert cmd["status"] == "done"
    ep_id = json.loads(cmd["result_json"])["episode_id"]
    ep = st.sql(f"SELECT * FROM episode WHERE id = {ep_id}")[0]
    assert ep["kind"] == "recovery_takeover" and ep["review_status"] == "pending"
    assert ep["takeover_ts"] and ep["handback_ts"] and ep["handback_ts"] >= ep["takeover_ts"]
    assert st.sql(f"SELECT * FROM dataset WHERE ticket_id = {tid}")[0]["total_count"] == 1


def test_injected_command_failure(stack, robot_id):
    st = stack
    tid = new_ticket(st, robot_id, "failure")
    cmd = _finished(st, _issue(st, robot_id, tid, "home", {"fake_fail_command": True}))
    assert cmd["status"] == "failed" and "[FAKE]" in json.loads(cmd["result_json"])["error"]
    _online(st, robot_id)


def test_soft_estop_preempts_running_eval(stack, robot_id):
    st, c = stack, stack.client()
    tid = new_ticket(st, robot_id, "estop")
    c.call_reducer("set_ticket_status", tid, "baseline", "test.tdm28")
    c.call_reducer("start_eval_run", tid, "fake://pi05_base", 10)
    run = st.newest("eval_run", f"ticket_id = {tid}")
    # fake_time_scale 2: each simulated 15-40 s trial takes 7-20 s of wall clock, so we stop mid-run.
    cid = _issue(st, robot_id, tid, "run_eval", {"eval_run_id": run["id"], "fake_time_scale": 2})
    wait_for(lambda: _cmd(st, cid)["status"] == "acked", 10, "eval acked")
    estop = _issue(st, robot_id, tid, "soft_estop")
    assert _cmd(st, cid)["status"] == "preempted"
    assert _finished(st, estop)["status"] == "done"
    wait_for(lambda: st.sql(f"SELECT * FROM robot WHERE id = {robot_id}")[0]["status"] == "estop", 10, "robot estop")
    _online(st, robot_id)  # --estop-clear-seconds 1 in the fixture
    n = len(st.sql(f"SELECT * FROM eval_trial WHERE eval_run_id = {run['id']}"))
    assert n < 10, "the preempted eval stopped early"
    # After the clear, the robot takes jobs again.
    assert _finished(st, _issue(st, robot_id, tid, "home"))["status"] == "done"
