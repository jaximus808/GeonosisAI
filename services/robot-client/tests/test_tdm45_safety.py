"""TDM-45 safety layer tests, against SimArm (no hardware), including adversarial inputs.

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm45_safety.py -q

Most tests drive the layer with a fake clock (exact 30 Hz ticks), so the speed / acceleration
assertions are deterministic. The watchdog and e-stop latency tests use real time.
"""
from __future__ import annotations

import dataclasses
import itertools
import math
import random
import threading
import time

import pytest

from rdf_robot_client.adapters import ArmError, ArmStopped, Pose, make_adapter
from rdf_robot_client.adapters.sim import SimArm
from rdf_robot_client.safety import (
    SafetyConfigError,
    SafetyLayer,
    SafetyViolation,
    Workspace,
    load_safety_config,
)

DT = 1.0 / 30.0


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, dt: float = DT) -> None:
        self.t += dt


class FakeYam(SimArm):
    """SimArm kinematics under the real-hardware name, to exercise the yam profile."""

    name = "yam"


def _layer(cfg_changes: dict | None = None, *, arm: SimArm | None = None, profile: str = "sim",
           watchdog: bool = False, clock: FakeClock | None = None, sink=None) -> tuple[SafetyLayer, SimArm, FakeClock]:
    inner = arm or SimArm()
    cfg = load_safety_config(profile)
    if cfg_changes:
        cfg = dataclasses.replace(cfg, **cfg_changes)
    clk = clock or FakeClock()
    layer = SafetyLayer(inner, cfg, clock=clk, start_watchdog=watchdog, event_sink=sink)
    layer.connect()
    return layer, inner, clk


@pytest.fixture
def safe():
    layer, inner, clk = _layer()
    yield layer, inner, clk
    layer.disconnect()


def _stream(layer: SafetyLayer, clk: FakeClock, targets, gripper=None) -> list[tuple[float, ...]]:
    out = []
    for q in targets:
        clk.tick()
        layer.command_joints(q, gripper)
        out.append(layer.last_joint_cmd)
    return out


# --------------------------------------------------------------------------- config


def test_profiles_load_and_validate():
    yam = load_safety_config("yam")
    sim = load_safety_config("sim")
    assert yam.verified is False and yam.speed_scale <= 0.3 and len(yam.max_velocity) == 6
    assert yam.workspace.lo[2] >= 0.03 - 1e-9  # table floor + clearance
    assert sim.verified is True
    with pytest.raises(SafetyConfigError):
        load_safety_config("no-such-arm")
    with pytest.raises(SafetyConfigError):
        dataclasses.replace(yam, speed_scale=2.0).validate()
    with pytest.raises(SafetyConfigError):
        dataclasses.replace(yam, max_velocity=(1.0,) * 5).validate(dof=6)


def test_unverified_profile_caps_speed_on_real_hardware():
    layer, _, _ = _layer(arm=FakeYam(), profile="yam")
    try:
        assert layer.speed_scale == pytest.approx(0.25)
        assert layer.set_speed_scale(1.0) == pytest.approx(0.3)  # unverified_max_speed_scale
        layer.set_slow_mode(True)
        assert layer.effective_scale == pytest.approx(0.3 * 0.3)
        with pytest.raises(ValueError):
            layer.set_speed_scale(float("nan"))
    finally:
        layer.disconnect()


def test_build_arm_wraps_sim_and_surfaces_counters():
    from rdf_robot_client.main import build_arm

    arm = build_arm("sim")
    assert isinstance(arm, SafetyLayer)
    arm.connect()
    try:
        assert arm.describe()["safety"]["profile"] == "sim"
        assert "rejected_jump" in arm.status().detail["safety"]
        assert arm.status().detail["sim"] is True  # adapter detail kept
    finally:
        arm.disconnect()


# --------------------------------------------------------------------------- adversarial setpoints


@pytest.mark.parametrize("bad", [
    [math.nan, 0, 0, 0, 0, 0],
    [0, math.inf, 0, 0, 0, 0],
    [0, 0, -math.inf, 0, 0, 0],
])
def test_nonfinite_joints_rejected_and_held(safe, bad):
    layer, inner, clk = safe
    before = inner.commands
    q = list(layer.home_joints)
    q = [a + b for a, b in zip(q, bad)]
    clk.tick()
    layer.command_joints(q)
    assert inner.commands == before  # nothing reached the adapter
    assert inner.status().holding
    assert layer.stats()["rejected_nonfinite"] == 1


def test_nonfinite_or_garbage_gripper_rejected(safe):
    layer, inner, clk = safe
    before = inner.commands
    clk.tick()
    layer.command_joints(layer.home_joints, gripper=math.nan)
    clk.tick()
    layer.command_joints(layer.home_joints, gripper="open")  # type: ignore[arg-type]
    layer.command_gripper(math.inf)
    assert inner.commands == before
    assert layer.stats()["rejected_nonfinite"] == 3


def test_wrong_length_is_an_error(safe):
    layer, _, _ = safe
    with pytest.raises(ArmError):
        layer.command_joints([0.0] * 5)


def test_huge_jump_rejected(safe):
    layer, inner, clk = safe
    q = list(layer.home_joints)
    q[0] += 2.0  # a bad policy chunk / teleop glitch
    clk.tick()
    layer.command_joints(q)
    assert layer.stats()["rejected_jump"] == 1
    assert inner.last_joint_cmd is None  # never sent
    assert inner.status().holding


def test_consecutive_rejects_raise(safe):
    layer, _, clk = safe
    bad = [math.nan] * 6
    with pytest.raises(SafetyViolation):
        for _ in range(layer.config.max_consecutive_rejects + 1):
            clk.tick()
            layer.command_joints(bad)
    assert layer.stats()["rejected_nonfinite"] == layer.config.max_consecutive_rejects
    # one good command resets the streak
    clk.tick()
    layer.command_joints(layer.home_joints)
    assert layer.stats()["consecutive_rejects"] == 0


def test_reject_action_raise():
    layer, _, clk = _layer({"reject_action": "raise"})
    try:
        clk.tick()
        with pytest.raises(SafetyViolation):
            layer.command_joints([math.nan] * 6)
    finally:
        layer.disconnect()


def test_joint_limits_with_margin_clamped():
    no_box = Workspace(False, (-math.inf,) * 3, (math.inf,) * 3)
    layer, inner, clk = _layer({"max_jump_rad": 100.0, "workspace": no_box})
    try:
        q = list(layer.home_joints)
        q[0] = 50.0  # far above joint 1's upper limit
        sent = _stream(layer, clk, [q] * 200)
        hi = inner.limits.upper[0] - layer.config.margin_rad
        assert all(s[0] <= hi + 1e-9 for s in sent)
        assert sent[-1][0] == pytest.approx(hi, abs=1e-6)  # converged to the limit, not past it
        assert layer.stats()["clamped_joint_limit"] >= 1
        assert layer.limits.upper[0] == pytest.approx(hi)
    finally:
        layer.disconnect()


def test_overspeed_is_scaled_to_velocity_and_accel_caps(safe):
    layer, _, clk = safe
    vmax = layer.config.max_velocity[0] * layer.effective_scale
    amax = layer.config.max_acceleration[0] * layer.effective_scale
    goal = list(layer.home_joints)
    goal[0] += 0.45  # within max_jump, but far more than one tick of motion
    sent = _stream(layer, clk, [goal] * 60)
    q0 = [layer.home_joints[0]] + [s[0] for s in sent]
    v = [(b - a) / DT for a, b in itertools.pairwise(q0)]
    assert v[0] <= amax * DT + 1e-9  # from rest: one tick of acceleration
    assert max(abs(x) for x in v) <= vmax + 1e-9
    speeding_up = [b - a for a, b in itertools.pairwise(v) if abs(b) > abs(a)]
    assert max(speeding_up) <= amax * DT + 1e-9
    assert sent[-1][0] == pytest.approx(goal[0], abs=1e-9)  # arrives, no overshoot
    assert max(q0) <= goal[0] + 1e-9
    assert layer.stats()["clamped_speed"] > 0


def test_speed_scale_halves_the_cap(safe):
    layer, _, clk = safe
    goal = list(layer.home_joints)
    goal[0] += 0.45
    layer.set_speed_scale(0.5)
    sent = _stream(layer, clk, [goal] * 40)
    q0 = [layer.home_joints[0]] + [s[0] for s in sent]
    vmax = layer.config.max_velocity[0] * 0.5
    assert max(abs(b - a) / DT for a, b in itertools.pairwise(q0)) <= vmax + 1e-9
    assert layer.limits.max_velocity[0] == pytest.approx(vmax)


def test_feasible_stream_passes_untouched(safe):
    """The --sim demo sweep (0.25 Hz, +-0.5 rad) fits the sim caps: no rejects, exact setpoints once
    the limiter has absorbed the sweep's velocity step at t=0 (it starts at 0.8 rad/s from rest)."""
    layer, _, clk = safe
    home = layer.home_joints
    targets = []
    for i in range(1, 300):
        t = i * DT
        q = list(home)
        q[0] += 0.5 * math.sin(2 * math.pi * 0.25 * t)
        q[1] += 0.2 * math.sin(2 * math.pi * 0.5 * t)
        targets.append(tuple(q))
    sent = _stream(layer, clk, targets, gripper=0.5)
    assert max(abs(a - b) for a, b in zip(sent[0], targets[0])) > 1e-3  # the step at t=0 was smoothed
    assert all(max(abs(a - b) for a, b in zip(s, q)) < 1e-9 for s, q in zip(sent[10:], targets[10:]))
    st = layer.stats()
    assert st["rejected_jump"] == st["rejected_nonfinite"] == st["clamped_workspace"] == 0


# --------------------------------------------------------------------------- workspace


def test_table_floor_holds_for_ee_targets_below_the_table():
    # tight floor 4 cm under the home tool point (home z ~= 0.084 m)
    ws = Workspace(True, (0.05, -0.55, 0.045), (0.70, 0.55, 0.65))
    layer, inner, clk = _layer({"workspace": ws})
    try:
        home = layer.ee_pose()
        z_min = ws.lo[2]
        for k in range(1, 120):  # teleop drags the tool down through the table at 3 cm/s
            clk.tick()
            target = home.translated((0.0, 0.0, -0.001 * k))
            try:
                layer.command_ee(target, seed=layer.last_joint_cmd)
            except ArmError:
                pass
            assert inner.fk(inner.last_joint_cmd).xyz[2] >= z_min - 1e-4
        assert layer.stats()["clamped_workspace"] > 0
        assert inner.fk(inner.last_joint_cmd).xyz[2] == pytest.approx(z_min, abs=5e-3)  # stopped AT the floor
    finally:
        layer.disconnect()


def test_out_of_box_targets_clamped_to_box():
    ws = Workspace(True, (0.30, -0.05, 0.05), (0.40, 0.05, 0.20))  # small box around home
    layer, inner, clk = _layer({"workspace": ws, "max_jump_rad": 100.0})
    try:
        rng = random.Random(45)
        for _ in range(400):
            clk.tick()
            q = [h + rng.uniform(-1.5, 1.5) for h in layer.home_joints]
            layer.command_joints(q)
            xyz = inner.fk(inner.last_joint_cmd).xyz
            assert ws.violation(xyz) <= 1e-6, xyz
        assert layer.stats()["clamped_workspace"] > 0
    finally:
        layer.disconnect()


def test_arm_outside_box_may_only_move_back_in():
    ws = Workspace(True, (0.40, -0.05, 0.05), (0.60, 0.05, 0.30))  # home (x 0.348) is OUTSIDE (x too small)
    layer, inner, clk = _layer({"workspace": ws, "max_jump_rad": 100.0})
    try:
        start = ws.violation(layer.ee_pose().xyz)
        assert start > 0
        q_out = list(layer.home_joints)
        q_out[2] += 0.4  # fold the elbow: tool comes back toward the base, further out of the box
        _stream(layer, clk, [q_out] * 30)
        assert ws.violation(inner.fk(inner.last_joint_cmd).xyz) <= start + 1e-6
        q_in = list(layer.home_joints)
        q_in[2] -= 0.5  # extend: tool moves out toward the box
        _stream(layer, clk, [q_in] * 60)
        assert ws.violation(inner.fk(inner.last_joint_cmd).xyz) < start
    finally:
        layer.disconnect()


# --------------------------------------------------------------------------- gripper


def test_gripper_range_clamped():
    layer, inner, clk = _layer({"gripper_min": 0.1, "gripper_max": 0.8})
    try:
        clk.tick()
        layer.command_joints(layer.home_joints, gripper=1.0)
        assert inner._g_target == pytest.approx(0.8)
        layer.command_gripper(-5.0)
        assert inner._g_target == pytest.approx(0.1)
        assert layer.stats()["clamped_gripper"] == 2
    finally:
        layer.disconnect()


# --------------------------------------------------------------------------- watchdog


def test_watchdog_fake_clock(safe):
    layer, inner, clk = safe
    clk.tick()
    layer.command_joints(layer.home_joints)
    clk.tick(0.15)
    assert layer.check_watchdog() is False
    clk.tick(0.06)  # 210 ms of silence
    assert layer.check_watchdog() is True
    assert inner.status().holding
    assert layer.check_watchdog() is False  # disarmed until the next command
    assert layer.stats()["watchdog_holds"] == 1


def test_watchdog_holds_within_200ms_real_time():
    inner = SimArm()
    layer = SafetyLayer(inner, load_safety_config("sim"))
    layer.connect()
    try:
        q = list(layer.home_joints)
        for i in range(15):  # stream a slow motion at 30 Hz for 0.5 s: no trip
            q[0] += 0.01
            layer.command_joints(q)
            t_last = time.monotonic()
            time.sleep(DT)
        assert layer.stats()["watchdog_holds"] == 0
        # the stream is cut mid-motion
        while layer.stats()["watchdog_holds"] == 0 and time.monotonic() - t_last < 1.0:
            time.sleep(0.005)
        dt_ms = (time.monotonic() - t_last) * 1000
        assert layer.stats()["watchdog_holds"] == 1
        assert 195 <= dt_ms <= 260, dt_ms
        assert inner.status().holding
        p1 = inner.joint_positions()
        time.sleep(0.1)
        assert max(abs(a - b) for a, b in zip(p1, inner.joint_positions())) < 1e-6  # not moving
        # a blocking move never trips it (no gaps the watchdog should care about)
        assert layer.home(speed_fraction=1.0)
        time.sleep(0.3)
        assert layer.stats()["watchdog_holds"] == 1
    finally:
        layer.disconnect()


# --------------------------------------------------------------------------- soft stop


def test_soft_stop_latches_in_layer_and_adapter(safe):
    layer, inner, clk = safe
    layer.soft_estop()
    assert layer.locked and inner.status().estopped and layer.status().estopped
    before = inner.commands
    with pytest.raises(ArmStopped):
        layer.command_joints(layer.home_joints)
    with pytest.raises(ArmStopped):
        layer.command_ee(layer.ee_pose())
    with pytest.raises(ArmStopped):
        layer.command_gripper(0.5)
    with pytest.raises(ArmStopped):
        layer.home()
    assert inner.commands == before
    layer.clear_stop()
    assert not layer.locked and not inner.status().estopped
    clk.tick()
    layer.command_joints(layer.home_joints)
    assert inner.commands == before + 1


def test_layer_latch_holds_even_if_adapter_forgets():
    class LeakyArm(SimArm):
        def stop(self) -> None:  # a buggy adapter that does not latch
            self.hold()

    layer, inner, _clk = _layer(arm=LeakyArm())
    try:
        layer.stop()
        with pytest.raises(ArmStopped):
            layer.command_joints(layer.home_joints)
        assert inner.last_joint_cmd is None
    finally:
        layer.disconnect()


def test_stop_from_another_thread_mid_stream_is_fast():
    inner = SimArm()
    layer = SafetyLayer(inner, load_safety_config("sim"))
    layer.connect()
    errors: list[BaseException] = []

    def stream() -> None:
        q = list(layer.home_joints)
        try:
            for _ in range(300):
                q[0] += 0.02
                layer.command_joints(q)
                time.sleep(DT)
        except ArmStopped as e:
            errors.append(e)

    th = threading.Thread(target=stream)
    th.start()
    time.sleep(0.3)
    t0 = time.monotonic()
    layer.stop()
    assert (time.monotonic() - t0) < 0.05
    th.join(2.0)
    try:
        assert errors and isinstance(errors[0], ArmStopped)
        p = inner.joint_positions()
        time.sleep(0.1)
        assert max(abs(a - b) for a, b in zip(p, inner.joint_positions())) < 1e-6
    finally:
        layer.disconnect()


def test_resume_after_hold_starts_from_measured_position(safe):
    """After a hold (watchdog, pause), a stale far target is a jump, not a lunge."""
    layer, _inner, clk = safe
    q = list(layer.home_joints)
    for _ in range(10):
        q[0] += 0.04
        clk.tick()
        layer.command_joints(q)
    layer.hold()
    stale = list(q)
    stale[0] += 0.6  # the policy kept integrating while we were paused
    clk.tick()
    layer.command_joints(stale)
    assert layer.stats()["rejected_jump"] == 1


# --------------------------------------------------------------------------- events + fuzz


def test_limit_events_are_rate_limited():
    events: list[tuple[str, dict]] = []
    layer, _, clk = _layer({"gripper_max": 0.8}, sink=lambda t, **p: events.append((t, p)))
    try:
        for _ in range(20):
            clk.tick()
            layer.command_joints(layer.home_joints, gripper=1.0)
        assert [e[0] for e in events] == ["robot.limit_hit"]
        clk.tick(6.0)
        layer.command_joints(layer.home_joints, gripper=1.0)
        assert len(events) == 2 and events[1][1]["suppressed"] == 19 and events[1][1]["total"] == 21
    finally:
        layer.disconnect()


def test_fuzz_invariants():
    """Random garbage stream: every setpoint that reaches the adapter obeys every limit."""
    layer, inner, clk = _layer()
    rng = random.Random(4545)
    lo, hi = layer.limits.lower, layer.limits.upper
    ws = layer.config.workspace
    vmax = layer._vmax()
    prev = None
    try:
        for _ in range(3000):
            clk.tick(rng.choice([DT, DT, DT, 0.0, 0.001, 0.2]))
            base = list(inner.last_joint_cmd or layer.home_joints)
            kind = rng.random()
            if kind < 0.1:
                q = [rng.choice([math.nan, math.inf, -math.inf, 0.0]) for _ in base]
            elif kind < 0.2:
                q = [b + rng.uniform(-20, 20) for b in base]
            else:
                q = [b + rng.uniform(-0.45, 0.45) for b in base]
            g = rng.choice([None, 0.5, -3.0, 7.0, math.nan])
            before = inner.last_joint_cmd
            try:
                layer.command_joints(q, g)
            except SafetyViolation:
                layer.clear_stop()  # the streak raised; start again
                prev = None
                continue
            sent = inner.last_joint_cmd
            if sent is None or sent is before:
                prev = None  # rejected: held
                continue
            assert all(a - 1e-9 <= v <= b + 1e-9 for v, a, b in zip(sent, lo, hi))
            assert ws.violation(inner.fk(sent).xyz) <= 1e-6
            if prev is not None:
                assert all(abs(s - p) <= vm * 2 * DT + 1e-9 for s, p, vm in zip(sent, prev, vmax))
            prev = sent
            assert 0.0 <= inner._g_target <= 1.0
        st = layer.stats()
        assert st["rejected_nonfinite"] > 0 and st["rejected_jump"] > 0 and st["sent"] > 100
    finally:
        layer.disconnect()


def test_command_ee_goes_through_the_layer(safe):
    layer, inner, clk = safe
    home = layer.ee_pose()
    clk.tick()
    layer.command_ee(Pose(home.xyz, home.quat))
    assert layer.stats()["sent"] == 1 and inner.commands == 1


def test_make_adapter_still_unwrapped():
    assert not isinstance(make_adapter("sim"), SafetyLayer)
