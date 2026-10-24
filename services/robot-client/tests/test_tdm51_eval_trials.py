"""TDM-51 eval trial runner tests (no SpacetimeDB): SimArm behind the safety layer, the TDM-50 fake
policy server, a fake reducer client, and a scripted "operator" that gives verdicts through the
same OutcomeInbox the terminal / ``trial_outcome`` command use. Every outcome here is SIM.

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm51_eval_trials.py -v

The end-to-end proof against a local SpacetimeDB + the TDM-67 orchestrator side is
``spikes/tdm-51-eval/prove_eval_sim.py``.
"""
from __future__ import annotations

import threading
import time

import pytest

from rdf_robot_client.commands import Cancelled
from rdf_robot_client.eval_trials import NOT_CONFIRMED, InfraFailure, OutcomeInbox, inbox_for, run_baseline, run_eval
from rdf_robot_client.fake_policy_server import Behaviour, FakePolicyServer, jitter_schedule, scripted_schedule
from rdf_robot_client.jobs import ResetDone
from rdf_robot_client.loop import RobotClient as _RC
from rdf_robot_client.policy_client import BASE_CHECKPOINT, PolicyClient, PolicyConfig
from rdf_robot_client.rollout import StandaloneRobot
from rdf_robot_client.state import RobotState

V = (0.3, -0.15, 0.1, 0.0, 0.0, 0.0)


class FakeHttp:
    """record_eval_trial with the module's rules (running run, index range, no duplicates)."""

    def __init__(self, trials: int) -> None:
        self.trials = trials
        self.rows: list[dict] = []
        self.calls: list[tuple] = []

    def call_reducer(self, name, *args):
        self.calls.append((name, args))
        if name == "record_eval_trial":
            run_id, idx, success, ms, ep, note = args
            if idx >= self.trials:
                raise RuntimeError("out of range")
            if any(r["trial_index"] == idx for r in self.rows):
                raise RuntimeError(f"eval run {run_id} already has trial {idx}")
            self.rows.append({"eval_run_id": run_id, "trial_index": idx, "success": success, "duration_ms": ms,
                              "episode_id": ep, "failure_note": note})

    def sql(self, q):
        return list(self.rows) if "FROM eval_trial" in q else []


class FakeRobot(StandaloneRobot):
    _add_reset_waiter = _RC._add_reset_waiter
    _remove_reset_waiter = _RC._remove_reset_waiter
    _release_resets = _RC._release_resets

    def __init__(self, arm, services, trials: int) -> None:
        super().__init__(arm, services)
        self.sub = None
        self._reset_waiters = []
        self._reset_lock = threading.Lock()
        self._consumed_resets = set()
        self.http = FakeHttp(trials)
        self.resets = 0


class Operator(threading.Thread):
    """Scripted stand-in for the human: resets the scene (resume_policy) and gives verdicts.

    plan[i] = ("during", success, note) -> verdict ~0.6 s into the rollout (ends it early)
              ("after", success, note)  -> verdict once the robot asks for it
              ("none",)                 -> never answers (the runner must record a failure)
    """

    def __init__(self, robot: FakeRobot, plan: list[tuple]) -> None:
        super().__init__(daemon=True)
        self.robot, self.plan = robot, plan
        self.stop = threading.Event()
        self.on_rollout = None  # optional hook(trial_index) while the rollout runs

    def run(self) -> None:
        box = inbox_for(self.robot)
        done: set[int] = set()
        seen_events = 0
        while not self.stop.is_set():
            r = self.robot
            if r.sm.state is RobotState.WAITING_RESET and r._reset_waiters:
                time.sleep(0.2)
                r.resets += 1
                r._release_resets(ResetDone("resume_policy", None))
            i = box.index
            if i is not None and i not in done and i < len(self.plan):
                how = self.plan[i][0]
                asked = any(t == "eval.trial_awaiting_outcome" and p.get("index") == i for t, p in r.events[seen_events:])
                if how == "during" and r.sm.state is RobotState.ROLLOUT:
                    time.sleep(0.6)
                    box.submit(self.plan[i][1], self.plan[i][2], "test operator", index=i)
                    done.add(i)
                elif how == "after" and asked:
                    box.submit(self.plan[i][1], self.plan[i][2], "test operator", index=i)
                    done.add(i)
                elif how == "hook" and r.sm.state is RobotState.ROLLOUT and self.on_rollout:
                    self.on_rollout(i)
                    done.add(i)
            time.sleep(0.02)


@pytest.fixture()
def sim_arm():
    from rdf_robot_client.main import build_arm

    arm = build_arm("sim")
    arm.connect()
    yield arm
    arm.disconnect()


def _setup(arm, server, trials):
    robot = FakeRobot(arm, {}, trials)
    pc = PolicyClient(PolicyConfig(backend="spike", url=server.url if server else "http://127.0.0.1:9")).start()
    robot.services["policy"] = pc
    return robot, pc


def _payload(trials, **kw):
    return {"eval_run_id": 7, "checkpoint_uri": BASE_CHECKPOINT + "/params", "trials": trials, "timeout_s": 1.5,
            "kind": "baseline", "prompt": "pick up the cube", "outcome_timeout_s": 1.0, **kw}


def test_three_trials_operator_outcomes_and_resets(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 3)
    op = Operator(robot, [("during", True, ""), ("after", False, "dropped the cube"), ("none",)])
    op.start()
    try:
        ctx = robot.context(_payload(3), "run_eval")
        out = run_eval(ctx)
    finally:
        op.stop.set()
        pc.close()
        srv.stop()
    rows = sorted(robot.http.rows, key=lambda r: r["trial_index"])
    assert [r["trial_index"] for r in rows] == [0, 1, 2]
    assert [r["success"] for r in rows] == [True, False, False]
    assert rows[0]["duration_ms"] < 1400  # the verdict ended trial 0 before its 1.5 s limit
    assert rows[1]["failure_note"].startswith("[SIM] dropped the cube")
    assert NOT_CONFIRMED in rows[2]["failure_note"] and "timeout: rollout hit the 1.5s limit" in rows[2]["failure_note"]
    assert out["recorded"] == 3 and out["successes"] == 1 and out["unconfirmed"] == [2] and out["sim"] is True
    assert [o["outcome_source"] for o in out["outcomes"]] == ["test operator", "test operator", "unconfirmed"]
    assert robot.resets == 2  # waited for a reset between trials, not after the last one
    waits = [p for t, p in robot.events if t == "robot.waiting_reset"]
    assert len(waits) == 2
    # the arm went home before every trial
    assert sum(1 for tr in robot.sm.history if tr.dst is RobotState.HOMING) == 3


def test_timeout_fail_gets_auto_note_and_late_or_wrong_verdicts_are_refused(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 1)
    op = Operator(robot, [("after", False, "")])
    op.start()
    try:
        out = run_eval(robot.context(_payload(1), "run_eval"))
    finally:
        op.stop.set()
        pc.close()
        srv.stop()
    (row,) = robot.http.rows
    assert row["success"] is False and "timeout: rollout hit the 1.5s limit" in row["failure_note"]
    assert out["outcomes"][0]["ended"] == "max_seconds"
    box = inbox_for(robot)
    with pytest.raises(ValueError, match="no eval trial is waiting"):
        box.submit(True, "", "late click")  # trial already recorded: never changed


def test_inbox_rules():
    box = OutcomeInbox()
    with pytest.raises(ValueError):
        box.submit(True)
    box.open(3, 1)
    with pytest.raises(ValueError, match="open trial is 1"):
        box.submit(True, index=0)
    with pytest.raises(ValueError, match="eval run 4"):
        box.submit(True, eval_run_id=4)
    with pytest.raises(ValueError, match="success"):
        box.submit("yes")  # type: ignore[arg-type]
    assert box.submit(False, "missed", "t", eval_run_id=3, index=1)["success"] is False
    with pytest.raises(ValueError, match="already has a verdict"):
        box.submit(True)


def test_estop_mid_trial_records_nothing_for_it(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 3)
    ctx = robot.context(_payload(3, timeout_s=5.0), "run_eval")
    op = Operator(robot, [("during", True, ""), ("hook",)])

    def estop(i):
        time.sleep(0.5)
        sim_arm.stop()  # what loop._estop_now does: arm first, then latch, then cancel the job
        robot.sm.lock("test soft_estop")
        ctx.cancel.cancel("estop", "test")

    op.on_rollout = estop
    op.start()
    try:
        with pytest.raises(Cancelled):
            run_eval(ctx)
    finally:
        op.stop.set()
        pc.close()
        srv.stop()
    assert [r["trial_index"] for r in robot.http.rows] == [0]  # trial 1 was interrupted: no row
    assert robot.sm.locked
    ev = [p for t, p in robot.events if t == "eval.trial_interrupted"]
    assert ev and ev[0]["index"] == 1 and ev[0]["reason"] == "estop"
    assert len(ctx.result["outcomes"]) == 1


def test_policy_error_mid_trial_is_a_failed_trial_and_dead_policy_fails_the_command(sim_arm):
    srv = FakePolicyServer(schedule=scripted_schedule([Behaviour(0.03)] * 8, Behaviour(0.02, "error")),
                           velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 2)
    op = Operator(robot, [("none",), ("none",)])
    op.start()
    try:
        with pytest.raises(InfraFailure, match="never produced an action"):
            run_eval(robot.context(_payload(2, timeout_s=10.0), "run_eval"))
    finally:
        op.stop.set()
        pc.close()
        srv.stop()
    (row,) = robot.http.rows
    assert row["trial_index"] == 0 and row["success"] is False and "policy_error" in row["failure_note"]
    assert row["duration_ms"] < 9000  # ended by the error, not the 10 s limit, with no operator wait


def test_intervened_trial_is_a_failure(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 1)
    op = Operator(robot, [("hook",)])

    def takeover(i):
        time.sleep(0.3)
        robot.sm.transition(RobotState.PAUSED_FOR_TAKEOVER, "test pause_for_takeover")
        time.sleep(0.3)
        robot.sm.transition(RobotState.ROLLOUT, "test handback")

    op.on_rollout = takeover
    op.start()
    try:
        run_eval(robot.context(_payload(1), "run_eval"))
    finally:
        op.stop.set()
        pc.close()
        srv.stop()
    (row,) = robot.http.rows
    assert row["success"] is False and row["failure_note"].startswith("[SIM] [intervened]")


def test_run_eval_needs_an_eval_run_and_run_baseline_without_one_is_a_single_episode(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 1)
    try:
        with pytest.raises(ValueError, match="eval_run_id"):
            run_eval(robot.context({"trials": 3}, "run_eval"))
        out = run_baseline(robot.context({"prompt": "pick up the cube", "timeout_s": 1.0}, "run_baseline"))
    finally:
        pc.close()
        srv.stop()
    assert out["rollout"]["ended"] == "max_seconds" and robot.http.rows == []


# --------------------------------------------------------------------------- TDM-69 collection rollouts

from rdf_robot_client.commands import Command  # noqa: E402
from rdf_robot_client.interventions import FAILURE_EVENT, HANDBACK_EVENT, advertise  # noqa: E402

S = RobotState


def _collect_payload(**iv):
    return {"purpose": "collect_recovery", "eval_run_id": 0, "trials": 1, "timeout_s": 1.5, "kind": "collect_recovery",
            "checkpoint_uri": BASE_CHECKPOINT + "/params", "prompt": "pick up the cube", "outcome_timeout_s": 3.0,
            "interventions": {"enabled": True, "trial_timeout_s": 0.8, "verdict_wait_s": 3.0, **iv}}


def _control(ctx, type_, payload, cid, *, state_first=False):
    """What loop._run_control does: the command lands on ctx.signals, then the state changes."""
    dst = S.PAUSED_FOR_TAKEOVER if type_ == "pause_for_takeover" else S.ROLLOUT
    c = Command(id=cid, seq=cid, robot_id=0, ticket_id=0, type=type_, payload=payload, status="acked",
                issued_by="test", issued_at_us=None)
    if state_first:
        ctx.sm.transition(dst, f"test {type_}")
    ctx.signals.put(c)
    if not state_first:
        time.sleep(0.05)
        ctx.sm.transition(dst, f"test {type_}")


def _wait_event(robot, type_, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for t, p in list(robot.events):
            if t == type_:
                return p
        time.sleep(0.02)
    raise AssertionError(f"no {type_} event")


def _run_with(robot, ctx, script):
    t = threading.Thread(target=script, daemon=True)
    t.start()
    try:
        return run_baseline(ctx)
    finally:
        t.join(5)


def test_collect_detector_pause_handback_recovers_with_the_claimants_answer(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 1)
    ctx = robot.context(_collect_payload(), "run_baseline")

    def orchestrator_and_claimant():
        f = _wait_event(robot, FAILURE_EVENT)
        assert f["detector"] == "timeout" and f["ref"] == "iv:0:0:0" and f["eval_run_id"] == 0
        assert robot.sm.state is S.ROLLOUT  # holding for the verdict, still in rollout
        _control(ctx, "pause_for_takeover", {"ref": f["ref"], "task_id": 5}, 11)
        time.sleep(0.4)
        _control(ctx, "handback", {"ref": f["ref"], "task_id": 5, "success": True}, 12)

    try:
        out = _run_with(robot, ctx, orchestrator_and_claimant)
    finally:
        pc.close()
        srv.stop()
    assert robot.http.rows == []  # never an eval_trial row
    (o,) = out["outcomes"]
    assert o["intervention"]["phase"] == "handed_back" and o["intervention"]["task_id"] == 5
    assert o["intervention"]["detector"] == "timeout" and o["success"] is True and o["confirmed"] is True
    assert o["outcome_source"] == "handback" and o["intervention"]["handback_success"] is True
    assert o["note"].startswith("[SIM] [intervened] timeout") and "claimant at handback: recovered" in o["note"]
    assert not any(t == "eval.trial_awaiting_outcome" for t, _ in robot.events)  # no operator prompt
    assert sum(1 for t, _ in robot.events if t == "rollout.started") == 2  # the policy finished the trial
    assert any(t == "intervention.robot_paused" for t, _ in robot.events)
    assert not any(t == HANDBACK_EVENT for t, _ in robot.events)  # DB handback: the orchestrator saw it already
    ended = _wait_event(robot, "collect.trial_ended")
    assert ended["intervention"]["ref"] == "iv:0:0:0"


def test_collect_no_verdict_or_end_trial_is_a_failure_without_an_outcome_prompt(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 1)
    try:
        out = run_baseline(robot.context(_collect_payload(verdict_wait_s=0.4), "run_baseline"))
        ctx = robot.context(_collect_payload(), "run_baseline")

        def decline():
            f = _wait_event(robot, FAILURE_EVENT)
            robot.events.clear()
            _control(ctx, "resume_policy", {"end_trial": True, "ref": f["ref"], "reason": "declined: budget"}, 21)

        robot.events.clear()
        out2 = _run_with(robot, ctx, decline)
    finally:
        pc.close()
        srv.stop()
    assert out["outcomes"][0]["outcome_source"] == "no_verdict" and out["outcomes"][0]["success"] is False
    assert out2["outcomes"][0]["outcome_source"] == "end_trial"
    assert "declined: budget" in out2["outcomes"][0]["note"]
    assert not any(t == "eval.trial_awaiting_outcome" for t, _ in robot.events)


def test_collect_operator_flag_mid_rollout_and_control_socket_handback(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 1)
    ctx = robot.context({**_collect_payload(trial_timeout_s=2.0), "timeout_s": 2.0}, "run_baseline")

    def flag():
        _wait_event(robot, "rollout.started")
        time.sleep(0.5)
        _control(ctx, "pause_for_takeover", {"ref": "iv:0:0:0", "task_id": 8, "detector": "operator",
                                             "reason": "operator pressed Failing"}, 31)
        time.sleep(0.3)
        ctx.sm.transition(S.TELEOP, "test takeover via /control")
        time.sleep(0.3)
        _control(ctx, "handback", {"via": "/control", "success": False}, 0, state_first=True)  # TDM-52 synthetic

    try:
        out = _run_with(robot, ctx, flag)
    finally:
        pc.close()
        srv.stop()
    (o,) = out["outcomes"]
    assert o["intervention"]["detector"] == "operator" and o["intervention"]["handback_via"] == "/control"
    assert o["success"] is False and o["confirmed"] is True and o["intervention"]["handback_success"] is False
    assert not any(t == FAILURE_EVENT for t, _ in robot.events)  # the operator flagged it, no detector event
    assert not any(t == HANDBACK_EVENT for t, _ in robot.events)  # the TDM-52 hub appends it, not the runner
    assert sum(1 for t, _ in robot.events if t == "rollout.started") == 1  # the same rollout continued


def test_eval_runs_never_run_the_detector(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 1)
    op = Operator(robot, [("after", True, "")])
    op.start()
    try:
        out = run_eval(robot.context(_payload(1, interventions={"enabled": True, "trial_timeout_s": 0.3}), "run_eval"))
    finally:
        op.stop.set()
        pc.close()
        srv.stop()
    assert not any(t == FAILURE_EVENT for t, _ in robot.events)
    assert robot.http.rows[0]["success"] is True and "interventions_note" in out


def test_advertise_capability():
    cfg = {"cameras": "off", "capabilities": ["x"]}
    assert advertise(cfg, True)["capabilities"] == ["x", "interventions"]
    assert advertise(cfg, True)["capabilities"] == ["x", "interventions"]  # idempotent
    assert advertise(cfg, False)["capabilities"] == ["x"]
    assert "capabilities" not in advertise({"cameras": "off"}, False)


def test_collect_handback_without_an_answer_leaves_success_unknown(sim_arm):
    srv = FakePolicyServer(schedule=jitter_schedule(60, 10), velocity=V).start()
    robot, pc = _setup(sim_arm, srv, 1)
    ctx = robot.context(_collect_payload(), "run_baseline")

    def pause_and_hand_back():
        f = _wait_event(robot, FAILURE_EVENT)
        _control(ctx, "pause_for_takeover", {"ref": f["ref"], "task_id": 6}, 41)
        time.sleep(0.3)
        _control(ctx, "handback", {"ref": f["ref"]}, 42)  # e.g. the orchestrator after complete_task

    try:
        out = _run_with(robot, ctx, pause_and_hand_back)
    finally:
        pc.close()
        srv.stop()
    (o,) = out["outcomes"]
    assert o["intervention"]["phase"] == "handed_back" and o["intervention"]["handback_success"] is None
    assert o["confirmed"] is False and o["outcome_source"] == "unconfirmed" and "success unknown" in o["note"]
