"""TDM-49 integration tests: the real command loop + SimArm against a throwaway local SpacetimeDB.

Boots ``spacetime start`` on 127.0.0.1:3049 (db ``tdm49loop``, data under $TMPDIR/tdm-49/) with the
TDM-28 harness's LocalStack, publishes the working-tree module, mints orchestrator and
robot_client identities, then drives the client the way the orchestrator/dashboard would:

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm49_loop.py -v

Never touches maincloud.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "integration"))

PORT = int(os.environ.get("TDM49_PORT", "3049"))
DB = "tdm49loop"
NAME = "test.tdm49-sim"


def wait_for(pred, timeout=10.0, what="condition", every=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture(scope="module")
def stack():
    if not shutil.which("spacetime"):
        pytest.skip("spacetime CLI not installed")
    from harness import LocalStack  # TDM-28

    st = LocalStack(port=PORT, db=DB, root=Path(tempfile.gettempdir()) / "tdm-49")
    try:
        st.start_server()
        st.publish()
        st.make_identity("orchestrator")
        st.make_identity("robot_client")
        yield st
    finally:
        st.stop()


@pytest.fixture(scope="module")
def orch(stack):
    return stack.client("orchestrator")


@pytest.fixture(scope="module")
def robot_status_log(stack):
    """Every robot.status value the orchestrator's view sees, in order (live subscription)."""
    from rdf_common.spacetime_client import Subscription

    seen: list[str] = []

    def on_up(table, old, new, meta):
        if new["name"] == NAME and new["status"] != old["status"]:
            seen.append(new["status"])

    sub = Subscription(["SELECT * FROM robot"], on_update=on_up, host=stack.host, db=DB,
                       token=stack.token("orchestrator")).start_thread()
    assert sub.wait_ready(15)
    yield seen
    sub.stop()


def make_client(stack, **kw):
    from rdf_common.spacetime_client import SpacetimeConfig

    from rdf_robot_client.adapters import make_adapter
    from rdf_robot_client.handlers import sim_demo_rollout
    from rdf_robot_client.loop import RobotClient

    cfg = SpacetimeConfig.resolve(stack.host, DB, stack.token("robot_client"))
    opts = dict(heartbeat_s=1.0, clear_estop_on_boot=True,
                handlers={"run_eval": sim_demo_rollout, "run_baseline": sim_demo_rollout})
    opts.update(kw)
    return RobotClient(make_adapter("sim"), name=NAME, stdb=cfg, camera_config={"sim": True}, **opts)


@pytest.fixture
def client(stack):
    c = make_client(stack).start()
    yield c
    c.stop()


def issue(orch, rid, type_, payload=None, ticket_id=0) -> int:
    orch.call_reducer("issue_robot_command", rid, type_, json.dumps(payload or {}), ticket_id)
    rows = orch.sql(f"SELECT * FROM robot_command WHERE robot_id = {rid}")
    return max(int(r["id"]) for r in rows if r["type"] == type_)


def cmd(orch, cid) -> dict:
    return orch.sql(f"SELECT * FROM robot_command WHERE id = {cid}")[0]


def wait_cmd(orch, cid, statuses, timeout=10.0) -> dict:
    statuses = {statuses} if isinstance(statuses, str) else set(statuses)
    return wait_for(lambda: (r := cmd(orch, cid))["status"] in statuses and r, timeout, f"command {cid} -> {statuses}")


def robot_row(orch) -> dict:
    return orch.sql(f"SELECT * FROM robot WHERE name = '{NAME}'")[0]


def moved(arm, seconds=0.15) -> float:
    a = arm.joint_positions()
    time.sleep(seconds)
    return max(abs(x - y) for x, y in zip(a, arm.joint_positions()))


# --------------------------------------------------------------------------- tests


def test_register_and_heartbeat(client, orch):
    row = robot_row(orch)
    assert int(row["id"]) == client.robot_id
    assert row["embodiment"] == "sim_yam" and row["status"] == "online"
    assert json.loads(row["camera_config_json"])["arm"]["sim"] is True
    hb = row["last_heartbeat"]
    wait_for(lambda: robot_row(orch)["last_heartbeat"] > hb, 4, "a periodic heartbeat")


def test_home_flips_busy_then_online(client, orch, robot_status_log):
    arm = client.arm
    off = list(arm.home_joints)
    off[0] += 0.8
    arm.move_joints(off, speed_fraction=1.0)
    robot_status_log.clear()
    cid = issue(orch, client.robot_id, "home")
    wait_for(lambda: client.sm.state.value == "homing", 5, "homing")
    row = wait_cmd(orch, cid, "done")
    assert json.loads(row["result_json"])["homed"] is True
    assert max(abs(a - b) for a, b in zip(arm.joint_positions(), arm.home_joints)) < 0.01
    wait_for(lambda: robot_status_log[-2:] == ["busy", "online"], 5, f"busy -> online (saw {robot_status_log})")
    def states():
        return {(json.loads(e["payload_json"])["from"], json.loads(e["payload_json"])["to"])
                for e in orch.sql("SELECT * FROM event_log WHERE type = 'robot.state_changed'")
                if json.loads(e["payload_json"]).get("command_id") == cid}

    wait_for(lambda: {("idle", "homing"), ("homing", "idle")} <= states(), 5, "state_changed events")


def test_soft_estop_mid_rollout_stops_within_200ms(client, orch, robot_status_log):
    arm = client.arm
    cid = issue(orch, client.robot_id, "run_eval", {"sim_seconds": 30})
    wait_for(lambda: client.sm.state.value == "rollout" and arm.status().holding is False, 5, "rollout streaming")
    time.sleep(0.5)
    assert moved(arm) > 0.01
    t0 = time.monotonic()
    orch.call_reducer("issue_robot_command", client.robot_id, "soft_estop", "{}", 0)
    wait_for(lambda: arm.last_stop_at is not None and arm.last_stop_at >= t0, 1.0, "arm stopped", every=0.002)
    latency = arm.last_stop_at - t0  # includes the issuing HTTP round-trip
    print(f"\nsoft_estop: issue call -> arm stopped {latency * 1000:.1f} ms; client view {client.last_estop}")
    assert latency < 0.2
    assert moved(arm) < 1e-6
    assert cmd(orch, cid)["status"] == "preempted"
    est = max((r for r in orch.sql(f"SELECT * FROM robot_command WHERE robot_id = {client.robot_id}")
               if r["type"] == "soft_estop"), key=lambda r: int(r["id"]))
    wait_cmd(orch, int(est["id"]), "done")
    assert client.sm.state.value == "locked"
    wait_for(lambda: robot_row(orch)["status"] == "estop", 5, "robot.status estop")
    # the preempted eval is never completed or restarted
    time.sleep(0.5)
    assert cmd(orch, cid)["status"] == "preempted"
    with pytest.raises(Exception):
        issue(orch, client.robot_id, "home")  # module refuses while estop
    assert client.clear_estop(by="test")
    wait_for(lambda: robot_row(orch)["status"] == "online", 5, "online after clear")
    assert client.sm.state.value == "idle" and client.current_job is None
    assert moved(arm) < 1e-6  # clearing does not restart anything


def test_pause_for_takeover_resume_and_stop(client, orch):
    arm = client.arm
    rid = client.robot_id
    cid = issue(orch, rid, "run_eval", {"sim_seconds": 30})
    wait_for(lambda: client.sm.state.value == "rollout", 5, "rollout")
    time.sleep(0.3)
    p = issue(orch, rid, "pause_for_takeover")
    wait_cmd(orch, p, "done")
    assert client.sm.state.value == "paused_for_takeover"
    wait_for(lambda: robot_row(orch)["status"] == "paused", 5, "robot.status paused")
    time.sleep(0.1)
    assert moved(arm) < 1e-6  # held
    r = issue(orch, rid, "resume_policy")
    wait_cmd(orch, r, "done")
    wait_for(lambda: moved(arm, 0.1) > 1e-3, 3, "moving again")
    assert client.sm.state.value == "rollout"
    s = issue(orch, rid, "stop")
    wait_cmd(orch, s, "done")
    row = wait_cmd(orch, cid, "done")
    assert json.loads(row["result_json"])["aborted"] is True
    wait_for(lambda: client.sm.state.value == "idle" and robot_row(orch)["status"] == "online", 5, "idle/online")


def test_pause_outside_rollout_is_failed_not_run(client, orch):
    p = issue(orch, client.robot_id, "pause_for_takeover")
    row = wait_cmd(orch, p, "failed")
    assert "refused" in row["result_json"]
    assert client.sm.state.value == "idle"


def test_eval_waits_for_resume_policy_between_segments(client, orch):
    rid = client.robot_id
    cid = issue(orch, rid, "run_eval", {"sim_seconds": 1.0, "sim_reset_every_s": 0.5})
    wait_for(lambda: client.sm.state.value == "waiting_reset", 5, "waiting_reset")
    wait_for(lambda: robot_row(orch)["status"] == "paused", 5, "robot.status paused")
    time.sleep(1.0)
    assert client.sm.state.value == "waiting_reset"  # never continues on its own
    assert cmd(orch, cid)["status"] == "acked"
    r = issue(orch, rid, "resume_policy", {"reason": "reset_done"})
    wait_cmd(orch, r, "done")
    row = wait_cmd(orch, cid, "done")
    assert json.loads(row["result_json"])["resets"] == 1


def test_reset_request_job(client, orch):
    rid = client.robot_id
    cid = issue(orch, rid, "reset_request", {"reason": "test"})
    wait_for(lambda: client.sm.state.value == "waiting_reset", 5, "waiting_reset")
    r = issue(orch, rid, "resume_policy", {"reason": "reset_done"})
    row = wait_cmd(orch, cid, "done")
    assert json.loads(row["result_json"])["reset"]["how"] == "resume_policy"
    wait_cmd(orch, r, "done")
    assert client.sm.state.value == "idle"


def test_controls_acked_strictly_in_seq_order(client, orch):
    rid = client.robot_id
    job = issue(orch, rid, "run_eval", {"sim_seconds": 30})
    wait_for(lambda: client.sm.state.value == "rollout", 5, "rollout")
    ids = []
    for t in ("pause_for_takeover", "resume_policy", "pause_for_takeover", "handback", "stop"):
        ids.append(issue(orch, rid, t))
    rows = [wait_cmd(orch, i, ("done", "failed")) for i in ids]
    assert [r["status"] for r in rows] == ["done"] * 5, [r.get("result_json") for r in rows]
    seqs = [int(r["seq"]) for r in rows]
    acks = [int(r["acked_at"]) for r in rows]
    assert seqs == sorted(seqs) and acks == sorted(acks)
    wait_cmd(orch, job, "done")


def test_control_ticks_match_episode_format(client, orch):
    from rdf_common.episode_format import ACTION_COLUMNS, CONTROL_SOURCES

    ticks = []
    client.add_tick_listener(ticks.append)
    idle = client.control_tick()
    assert idle.control_source == "idle"
    cid = issue(orch, client.robot_id, "run_eval", {"sim_seconds": 0.5})
    wait_cmd(orch, cid, "done")
    client.remove_tick_listener(ticks.append)
    assert len(ticks) >= 10 and {t.control_source for t in ticks} == {"policy"}
    row = ticks[-1].to_row()
    assert set(row) <= set(ACTION_COLUMNS) and {t.control_source for t in ticks} <= set(CONTROL_SOURCES)
    assert len(row["joint_pos"]) == 6 and len(row["ik_target_quat"]) == 4 and row["joint_cmd"] is not None


def test_boot_never_runs_preempted_or_leftover_commands(stack, orch):
    c = make_client(stack).start()
    rid = c.robot_id
    arm_home = c.arm.home_joints
    c.stop()
    # 1) a job acked by a previous process is failed at boot, not resumed
    from rdf_common.spacetime_client import SpacetimeClient

    robot_http = SpacetimeClient(stack.host, DB, stack.token("robot_client"))
    lc = issue(orch, rid, "load_checkpoint", {"checkpoint_uri": "sim://x"})
    robot_http.call_reducer("ack_robot_command", lc)
    # 2) a command preempted by a soft_estop while the client was down never runs
    c2 = make_client(stack)
    off = list(arm_home)
    off[0] += 0.7
    c2.arm.connect()
    c2.arm.move_joints(off, speed_fraction=1.0)
    c2.start()
    try:
        row = wait_cmd(orch, lc, "failed")
        assert "restarted" in row["result_json"]
    finally:
        c2.stop()
    home = issue(orch, rid, "home")
    orch.call_reducer("issue_robot_command", rid, "soft_estop", "{}", 0)
    assert cmd(orch, home)["status"] == "preempted"
    c3 = make_client(stack, clear_estop_on_boot=False)
    c3.arm.connect()
    c3.arm.move_joints(off, speed_fraction=1.0)
    time.sleep(0.3)  # settle fully (move_joints returns within 0.01 rad)
    q_before = c3.arm.joint_positions()
    c3.start()
    try:
        wait_for(lambda: c3.sm.state.value == "locked", 5, "locked from the queued soft_estop")
        time.sleep(0.5)
        assert cmd(orch, home)["status"] == "preempted"
        assert max(abs(a - b) for a, b in zip(c3.arm.joint_positions(), q_before)) < 1e-6  # home never ran
        assert c3.clear_estop(by="test")
    finally:
        c3.stop()
    robot_http.close()


def test_stale_issued_command_is_failed_at_boot(stack, orch):
    c = make_client(stack).start()
    rid = c.robot_id
    c.stop()
    h = issue(orch, rid, "home")
    time.sleep(1.2)
    c2 = make_client(stack, max_command_age_s=1.0).start()
    try:
        row = wait_cmd(orch, h, "failed")
        assert "stale" in row["result_json"]
    finally:
        c2.stop()
