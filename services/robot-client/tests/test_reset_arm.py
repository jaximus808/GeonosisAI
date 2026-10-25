"""reset_arm on /ws/control: a slow joint-space ramp back to the rest pose (LARP's 'z' home on the YAM).

SimArm behind the SafetyLayer, the same bench as TDM-52. The rest pose here is the pose measured at start
(SimArm has no LARP home); the YAM target selection is checked on the sim YAM adapter.

    cd services/robot-client && uv run --with pytest pytest tests/test_reset_arm.py -v
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from rdf_robot_client import teleop
from rdf_robot_client.teleop import LARP_HOME_JOINTS, TeleopHub

from test_tdm52_teleop import Bench, Browser, rig, wait_for  # noqa: F401 - rig is a fixture

AWAY = (0.35, -0.3, 0.25, 0.2, -0.2, 0.3)  # rad from the boot pose


@pytest.fixture
def bench(rig, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(teleop, "LARP_HOME_DURATION_S", 0.5)  # keep the tests short; the speed cap still rules
    b = Bench(rig, tmp_path)
    b.hub.record_boot_pose()
    yield b
    b.close()


def _move_away(bench):
    q0 = bench.arm.joint_positions()
    bench.arm.move_joints(tuple(a + d for a, d in zip(q0, AWAY)), speed_fraction=1.0)
    bench.arm.hold()
    return q0


class Sampler:
    def __init__(self, arm):
        self.arm, self.samples, self._stop = arm, [], threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.samples.append((time.monotonic(), self.arm.joint_positions()))
            time.sleep(0.02)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.t.join(1)

    def max_speed(self, window_s=0.1):
        return max_speed(self.samples, window_s)


def max_speed(samples, window_s=0.1):
    """Peak per-joint speed over windows of at least ``window_s`` (thread jitter makes 20 ms windows noisy)."""
    v, j = 0.0, 0
    for i, (t0, q0) in enumerate(samples):
        j = max(j, i + 1)
        while j < len(samples) and samples[j][0] - t0 < window_s:
            j += 1
        if j >= len(samples):
            break
        t1, q1 = samples[j]
        v = max(v, max(abs(b - a) for a, b in zip(q0, q1)) / (t1 - t0))
    return v


def test_reset_ramps_home_under_speed_cap(bench):
    boot = _move_away(bench)
    bench.start_teleop_job()
    sent = []  # every setpoint that left the SafetyLayer for the adapter
    inner_cmd = bench.arm.inner.command_joints

    def spy(q, g=None):
        sent.append((time.monotonic(), tuple(q)))
        return inner_cmd(q, g)
    bench.arm.inner.command_joints = spy

    async def go():
        async with Browser(bench.url, bench.token()) as b:
            await b.cmd(clutch=False)
            with Sampler(bench.arm) as s:
                await b.send(t="reset_arm", req=11)
                mid = None
                end = time.monotonic() + 1.5
                while time.monotonic() < end:
                    if b.states and b.states[-1].get("resetting"):
                        mid = b.states[-1]
                    await asyncio.sleep(0.05)
                ack = await b.wait_msg(lambda m: m.get("op") == "reset_arm", timeout=20)
            return ack, mid, s

    ack, mid, s = asyncio.run(go())
    assert ack["ok"], ack
    assert ack["req"] == 11 and ack["source"] == "boot_pose" and ack["percent"] == 100
    assert mid is not None and 0 <= mid["reset"]["percent"] < 100 and mid["hold_reason"] == "resetting"
    assert max(abs(a - b) for a, b in zip(bench.arm.joint_positions(), boot)) < 0.06
    assert len(sent) > 30
    assert max_speed(sent) <= teleop.RESET_MAX_JOINT_SPEED * 1.1, max_speed(sent)  # the setpoints: <= 0.2 rad/s
    assert s.max_speed() <= 0.3, s.max_speed()  # the measured arm: under LarpSafeYamArm's 0.3 rad/s ceiling
    # planned from the speed cap, not LARP's 5 s floor (patched to 0.5 s here)
    assert ack["planned_s"] >= 1.5 * 0.35 / teleop.RESET_MAX_JOINT_SPEED - 0.02
    assert bench.robot.sm.state.value == "teleop"  # the session goes on


def test_stop_aborts_reset_and_holds(bench):
    _move_away(bench)
    bench.start_teleop_job()

    async def go():
        async with Browser(bench.url, bench.token()) as b:
            await b.send(t="reset_arm", req=1)
            await asyncio.sleep(0.6)
            await b.send(t="stop")
            ack = await b.wait_msg(lambda m: m.get("op") == "reset_arm", timeout=5)
            p = bench.arm.joint_positions()
            await asyncio.sleep(0.5)
            return ack, p

    ack, p = asyncio.run(go())
    assert not ack["ok"] and ack["code"] == "stopped", ack
    assert 0 < ack["percent"] < 100
    assert max(abs(a - b) for a, b in zip(p, bench.arm.joint_positions())) < 1e-3, "must hold where it stopped"
    assert bench.arm.locked


def test_reset_refused_while_recording(bench):
    bench.start_teleop_job()

    async def go():
        async with Browser(bench.url, bench.token()) as b:
            await b.send(t="record_start", req=1)
            a = await b.wait_msg(lambda m: m.get("op") == "record_start")
            assert a["ok"], a
            await b.send(t="reset_arm", req=2)
            r = await b.wait_msg(lambda m: m.get("op") == "reset_arm")
            await b.send(t="record_discard", req=3)
            await b.wait_msg(lambda m: m.get("op") == "record_discard", timeout=10)
            return r

    r = asyncio.run(go())
    assert not r["ok"] and r["code"] == "recording", r


def test_reset_refused_while_clutched(bench):
    bench.start_teleop_job()

    async def go():
        async with Browser(bench.url, bench.token()) as b:
            for _ in range(10):
                await b.cmd(d_pos=(0.0, 0.0, 0.0))
                await asyncio.sleep(1 / 30)
            await b.send(t="reset_arm", req=2)
            for _ in range(5):
                await b.cmd(d_pos=(0.0, 0.0, 0.0))
                await asyncio.sleep(1 / 30)
            return await b.wait_msg(lambda m: m.get("op") == "reset_arm")

    r = asyncio.run(go())
    assert not r["ok"] and r["code"] == "clutched", r


def test_reset_refused_for_non_holders(bench):
    bench.start_teleop_job()
    stranger = SimpleNamespace(revoked=None, claims=SimpleNamespace(sub="contributor:99", task_id=7))
    r = bench.hub.reset_arm_op(stranger, {})  # not the attached session
    assert not r["ok"] and r["code"] == "not_active"
    lapsed = SimpleNamespace(revoked="lease_expired", claims=SimpleNamespace(sub="contributor:3", task_id=7))
    r = bench.hub.reset_arm_op(lapsed, {})
    assert not r["ok"] and r["code"] == "lease_expired"
    assert bench.hub.counters.get("resets", 0) == 0


def test_disconnect_holds_and_never_resets(bench):
    """Dropping the socket mid-session only holds: no motion back to rest without an explicit reset."""
    bench.start_teleop_job()

    async def go():
        async with Browser(bench.url, bench.token()) as b:
            await b.drive(0.6, lambda t: [0.02 * min(t / 0.5, 1.0), 0.0, 0.0])
        await asyncio.sleep(0.2)
        p = bench.arm.joint_positions()
        await asyncio.sleep(0.6)
        return p

    p = asyncio.run(go())
    assert max(abs(a - b) for a, b in zip(p, bench.arm.joint_positions())) < 1e-4
    assert bench.hub.counters.get("resets", 0) == 0


def test_yam_target_is_larp_home_and_limits_are_checked(monkeypatch):
    from rdf_robot_client.adapters.yam_larp import LarpSafeYamArm

    arm = LarpSafeYamArm(sim=True)
    client = SimpleNamespace(arm=arm, services={})
    hub = TeleopHub(client)
    goal, source, (lo, hi) = hub._reset_target()
    assert source == "larp_home" and goal == LARP_HOME_JOINTS == (0.0,) * 6
    assert lo[1] == 0.0 and lo[2] == 0.0  # J2/J3 rest on their lower limit; the SafetyLayer margin lifts them
    hub.close()
