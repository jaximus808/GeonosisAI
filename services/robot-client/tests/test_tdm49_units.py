"""TDM-49 unit tests: SimArm, the adapter wrapper contract, the state machine, command ordering.

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm49_units.py -q
"""
from __future__ import annotations

import math
import threading
import time

import pytest

from rdf_robot_client.adapters import ArmAdapterWrapper, ArmStopped, EETarget, JointTarget, Pose, make_adapter
from rdf_robot_client.adapters.base import pose_error
from rdf_robot_client.commands import Command
from rdf_robot_client.state import DB_STATUS, InvalidTransition, RobotState, StateMachine

S = RobotState


@pytest.fixture
def arm():
    a = make_adapter("sim")
    a.connect()
    yield a
    a.disconnect()


def test_fk_ik_roundtrip(arm):
    home = arm.ee_pose()
    for d in [(0.05, -0.05, 0.03), (-0.04, 0.08, 0.05), (0.0, 0.0, -0.03)]:
        goal = home.translated(d)
        q = arm.ik(goal)
        dp, dr = pose_error(goal, arm.fk(q))
        assert dp < 1e-3 and dr < 5e-3
        assert arm.limits.within(q)


def test_rpy_quat_roundtrip():
    p = Pose.from_rpy((0.1, 0.2, 0.3), (0.3, -0.4, 1.1))
    assert all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(p.rpy, (0.3, -0.4, 1.1)))
    assert pose_error(p, Pose.from_matrix(p.matrix()))[1] < 1e-6


def test_move_and_velocity_limit(arm):
    goal = list(arm.home_joints)
    goal[0] += 1.0
    t0 = time.monotonic()
    assert arm.move_joints(goal, speed_fraction=1.0)
    assert time.monotonic() - t0 >= 1.0 / 3.0 - 0.05  # 1 rad at <= 3 rad/s
    assert max(abs(a - b) for a, b in zip(arm.joint_positions(), goal)) < 0.01


def test_stop_latches_and_is_fast(arm):
    goal = list(arm.home_joints)
    goal[0] += 2.0
    arm.command_joints(goal)
    time.sleep(0.1)
    t0 = time.monotonic()
    arm.stop()
    assert time.monotonic() - t0 < 0.01
    q_stop = arm.joint_positions()
    time.sleep(0.1)
    assert max(abs(a - b) for a, b in zip(arm.joint_positions(), q_stop)) < 1e-6  # frozen
    assert arm.status().estopped
    with pytest.raises(ArmStopped):
        arm.command_joints(goal)
    arm.clear_stop()
    arm.command_joints(goal)  # accepted again


def test_move_aborts_and_holds(arm):
    goal = list(arm.home_joints)
    goal[0] += 2.0
    flag = threading.Event()
    threading.Timer(0.15, flag.set).start()
    assert arm.move_joints(goal, speed_fraction=0.2, should_abort=flag.is_set) is False
    q = arm.joint_positions()
    time.sleep(0.1)
    assert max(abs(a - b) for a, b in zip(arm.joint_positions(), q)) < 1e-6


def test_clamps_out_of_limit_commands(arm):
    arm.command_joints([10.0] * 6)
    assert arm.status().detail["clamped"] == 1
    assert arm.last_joint_cmd == arm.limits.clamp([10.0] * 6)


class _Recording(ArmAdapterWrapper):
    """What TDM-45 will look like: override command_joints, everything funnels through it."""

    def __init__(self, inner):
        super().__init__(inner)
        self.seen = []

    def command_joints(self, joints, gripper=None):
        self.seen.append(tuple(joints))
        super().command_joints(joints, gripper)


def test_wrapper_sees_every_motion(arm):
    w = _Recording(arm)
    assert w.name == "sim" and w.dof == 6 and w.home_joints == arm.home_joints
    w.command_ee(arm.ee_pose().translated((0.02, 0, 0)))
    w.command(EETarget(arm.ee_pose().translated((0.0, 0.02, 0))))
    w.command(JointTarget(arm.home_joints))
    w.move_joints(arm.home_joints, duration=0.1)
    w.home(duration=0.1)
    assert len(w.seen) >= 5


def test_state_machine_transitions_and_db_status():
    sm = StateMachine()
    seen = []
    sm.add_listener(seen.append)
    assert sm.db_status() == "online"
    sm.transition(S.HOMING)
    assert sm.db_status() == "busy"
    with pytest.raises(InvalidTransition):
        sm.transition(S.ROLLOUT)  # homing -> rollout is not allowed
    sm.transition(S.IDLE)
    sm.transition(S.ROLLOUT)
    with pytest.raises(InvalidTransition):
        sm.transition(S.PAUSED_FOR_TAKEOVER, allowed_from=[S.TELEOP])
    sm.transition(S.PAUSED_FOR_TAKEOVER)
    assert sm.db_status() == "paused"
    sm.lock("test")
    assert sm.db_status() == "estop"
    with pytest.raises(InvalidTransition):
        sm.transition(S.IDLE)  # only unlock() leaves locked
    sm.unlock("cleared")
    assert sm.state is S.IDLE
    assert [str(t.dst) for t in seen] == ["homing", "idle", "rollout", "paused_for_takeover", "locked", "idle"]
    sm.job_active = True
    assert sm.db_status() == "busy"
    assert set(DB_STATUS.values()) <= {"offline", "online", "busy", "paused", "estop"}  # TDM-8 ROBOT_STATUSES


def test_command_priority_estop_first_then_seq():
    rows = [
        {"id": 5, "seq": 3, "type": "stop", "status": "issued"},
        {"id": 3, "seq": 1, "type": "home", "status": "issued"},
        {"id": 9, "seq": 4, "type": "soft_estop", "status": "issued"},
        {"id": 4, "seq": 2, "type": "pause_for_takeover", "status": "issued", "payload_json": "not json"},
    ]
    cmds = sorted((Command.from_row(r) for r in rows), key=lambda c: c.priority)
    assert [c.seq for c in cmds] == [4, 1, 2, 3]
    assert cmds[2].payload == {"_unparsed": "not json"}
    assert cmds[1].is_job and not cmds[2].is_job and cmds[0].is_estop
