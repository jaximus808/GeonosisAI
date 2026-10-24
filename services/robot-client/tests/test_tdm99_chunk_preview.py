"""TDM-99: the rollout publishes the chunk it is executing as ``chunk_preview`` on ``/ws/state``.

Part 1 (virtual clock): ``ChunkExecutor.preview`` is the downsampled REMAINING plan: its first row is
the next tick's target, its last the chunk's final step, and it goes away while holding or paused.
Part 2 (real time): during ``run_episode`` (SimArm + synthetic cameras + the fake policy server) the
state feed's sampler carries ``chunk_preview``; the rows lead the measured joints along the ramp, and
the service is removed when the episode ends.

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm99_chunk_preview.py -v
"""
from __future__ import annotations

import threading
import time

import numpy as np
from test_tdm50_rollout import DT, V, ScriptedPolicy, VClock, run_virtual, sim_robot  # noqa: F401 - fixture

from rdf_robot_client.fake_policy_server import FakePolicyServer, jitter_schedule
from rdf_robot_client.policy_client import PolicyClient, PolicyConfig
from rdf_robot_client.rollout import ExecConfig, run_episode
from rdf_robot_client.stream import LiveStateSampler


def test_preview_is_the_remaining_plan_downsampled():
    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (5, "ok"))
    seen = []

    def hook(i, ex, _pol):
        if i in (30, 31, 77):
            seen.append((i, ex.chunk is not None and ex.chunk[ex.pos].copy(), ex.chunk[-1].copy(),
                         ex.preview(points=8, now=clock.now)))

    ex, traj = run_virtual(pol, clock, 120, hooks=hook)
    for i, nxt, last, pv in seen:
        assert pv is not None and 2 <= len(pv) <= 8, (i, pv)
        assert np.allclose(pv[0], nxt) and np.allclose(pv[-1], last)  # next target .. end of the chunk
        assert all(len(r) == 7 for r in pv)  # 6 joints + gripper
        j0 = np.asarray([r[0] for r in pv])
        assert np.all(np.diff(j0) > 0)  # V[0] > 0: the plan runs ahead, monotonically, along the ramp
    # the plan leads what the arm did: the preview at tick 30 reaches where the arm is ~ticks later
    i, _, _, pv = seen[0]
    assert pv[-1][0] > traj[i][0]


def test_preview_none_without_chunk_when_holding_or_paused():
    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (None, "ok"))  # never answers: the arm holds from the start
    ex, _ = run_virtual(pol, clock, 20)
    assert ex.chunk is None and ex.preview(now=clock.now) is None

    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (5, "ok"))
    ex, _ = run_virtual(pol, clock, 40)
    assert ex.preview(now=clock.now) is not None
    # paused (takeover / reset): no tick for longer than gap_reset_s -> the chunk is the past
    assert ex.preview(now=clock.now + ExecConfig().gap_reset_s + 0.05) is None
    ex.pos = len(ex.chunk)  # ran out of chunk (stale-chunk hold)
    assert ex.preview(now=clock.now) is None


def test_state_feed_carries_chunk_preview_during_rollout(sim_robot):  # noqa: F811 - pytest fixture
    srv = FakePolicyServer(schedule=jitter_schedule(120, 10, seed=3), velocity=tuple(V)).start()
    sampler = LiveStateSampler(sim_robot, sim=True)
    got: list[dict] = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            got.append(sampler.sample())
            time.sleep(1 / 30)

    th = threading.Thread(target=watch, daemon=True)
    try:
        ctx = sim_robot.context({"prompt": "pick up the cube"})
        th.start()
        with PolicyClient(PolicyConfig(backend="spike", url=srv.url)) as pc:
            res = run_episode(ctx, "pick up the cube", timeout_s=3.0, policy=pc)
        time.sleep(0.15)
    finally:
        stop.set()
        th.join(2)
        srv.stop()
    assert res.ok and res.executor["chunks_used"] >= 3
    with_pv = [m for m in got if "chunk_preview" in m]
    assert len(with_pv) >= 30, f"{len(with_pv)} of {len(got)} samples carried chunk_preview"
    for m in with_pv:
        pv = m["chunk_preview"]
        assert 2 <= len(pv) <= 10 and all(len(r) == 7 for r in pv)
        assert m["control_source"] == "policy"
    # it leads the arm: the fake ramp moves joint 0 at +V[0], so the plan's end is ahead of the measured joint
    lead = [m["chunk_preview"][-1][0] - m["joints"][0] for m in with_pv]
    assert np.median(lead) > 0.5 * V[0] * DT * 20, np.median(lead)
    # the service is ours only during the episode
    assert "chunk_preview" not in sim_robot.services
    assert "chunk_preview" not in sampler.sample()
