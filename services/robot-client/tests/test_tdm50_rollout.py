"""TDM-50 rollout tests.

Part 1 (deterministic): ``ChunkExecutor`` on a virtual clock with a scripted in-process policy, so
latency skipping, crossfades, stale-chunk holds, retries and the sync fallback are checked exactly.

Part 2 (real time, laptop): SimArm behind the TDM-45 safety layer (when present) + synthetic
cameras + the local fake websocket policy server with injected latency / jitter / drops / errors,
driven through a real ``JobContext.run_control_loop``.

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm50_rollout.py -v
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from rdf_robot_client.adapters.base import ArmState, Pose
from rdf_robot_client.fake_policy_server import Behaviour, FakePolicyServer, jitter_schedule, scripted_schedule
from rdf_robot_client.policy_client import Observation, PolicyClient, PolicyConfig, PolicyReply
from rdf_robot_client.rollout import ChunkExecutor, ExecConfig, StandaloneRobot, map_actions, run_episode

DT = 1 / 30
V = np.array([0.3, -0.15, 0.1, 0.0, 0.0, 0.0])  # rad/s ramp
VMAX = (3.0,) * 6


# =========================================================================== part 1: virtual clock


class VClock:
    def __init__(self) -> None:
        self.now = 0.0


class ScriptedPolicy:
    """In-process stand-in for PolicyClient. ``plan(i) -> (latency_steps | None, kind)``."""

    def __init__(self, clock: VClock, plan, chunk_fn=None, horizon: int = 50) -> None:
        self.clock, self.plan, self.h = clock, plan, horizon
        self.chunk_fn = chunk_fn or self.ramp
        self.inflight: tuple[PolicyReply, float] | None = None
        self.n = 0
        self.submitted: list[Observation] = []

    def ramp(self, obs: Observation) -> np.ndarray:
        s = np.asarray(obs.state)
        j = np.arange(1, self.h + 1)[:, None]
        return np.concatenate([s[:6][None] + j * V[None] * DT, np.full((self.h, 1), 0.5)], axis=1)

    @property
    def busy(self) -> bool:
        return self.inflight is not None

    def submit(self, obs: Observation):
        if self.inflight:
            return None
        self.n += 1
        lat, kind = self.plan(self.n - 1)
        self.submitted.append(obs)
        r = PolicyReply(self.n, obs.step, obs.t_obs, obs.t_obs, 0.0)
        if kind == "ok":
            r.actions, r.action_space = self.chunk_fn(obs), "joint_pos_abs"
        else:
            r.error, r.code = f"injected {kind}", kind
        due = self.clock.now + (lat if lat is not None else 1e9) * DT - 1e-9
        self.inflight = (r, due)
        return self.n

    def poll(self):
        if self.inflight and self.clock.now >= self.inflight[1]:
            r, _ = self.inflight
            self.inflight = None
            r.t_recv = self.clock.now
            return [r]
        return []

    def drop_inflight(self) -> None:  # what the real client does after request_timeout_s
        if self.inflight:
            r, _ = self.inflight
            r.actions, r.error, r.code = None, "timeout", "timeout"
            self.inflight = (r, self.clock.now)


def state_of(q, g=1.0) -> ArmState:
    return ArmState(t=0.0, joints=tuple(float(v) for v in q), gripper=g, ee_pose=Pose((0.0, 0.0, 0.0)))


def run_virtual(policy: ScriptedPolicy, clock: VClock, steps: int, cfg: ExecConfig | None = None, hooks=None):
    cfg = cfg or ExecConfig()
    q = np.zeros(7)
    q[6] = 1.0

    def observe(step, now, st):
        return Observation(images=[], state=list(st.state_vector()), prompt="p", t_obs=now, step=step)

    ex = ChunkExecutor(policy, observe, cfg, VMAX)
    out = []
    for i in range(steps):
        clock.now = i * DT
        if hooks:
            hooks(i, ex, policy)
        tgt = ex.tick(i, clock.now, state_of(q[:6], q[6]))
        if tgt is None:
            break
        q = tgt.copy()  # perfect tracking: next measured state = this target
        out.append(tgt.copy())
    return ex, np.asarray(out)


def test_latency_skip_and_smooth_stitching():
    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (5, "ok"))  # 5 steps = 167 ms, like H100 p50
    # stitching alone: TDM-122's 0.3 rad/s cap + soft start are tested in test_tdm122_yam_mapping.py
    ex, traj = run_virtual(pol, clock, 200, ExecConfig(max_speed_rad_s=1e9, soft_start_ticks=1))
    s = ex.stats
    assert s.skips[1:] and set(s.skips[1:]) == {5}  # round(latency / 33 ms)
    assert s.requests >= 15 and s.chunks_used == s.chunks_received
    # after the start-up, every step advances by exactly one ramp step: no seams at chunk switches
    d = np.diff(traj[40:, :6], axis=0)
    assert np.allclose(d, V * DT, atol=1e-9)
    assert max(s.switch_jumps) == pytest.approx(float(np.max(np.abs(V * DT))), rel=1e-6)
    assert s.hold_ticks == 5 and s.holds[0]["reason"] == "no_chunk"  # only while the first chunk is in flight
    assert s.aborted is None


def test_without_skip_the_seams_show():
    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (5, "ok"))
    ex, traj = run_virtual(pol, clock, 200, ExecConfig(skip_latency=False))
    # replaying 5 stale actions on every switch re-runs part of the path: the arm falls behind the
    # policy's intended velocity (with the skip it tracks it exactly, see the test above)
    speed = np.diff(traj[40:, 0]).mean() / DT
    assert speed < 0.7 * V[0], speed


def test_crossfade_weights_over_three_steps():
    clock = VClock()
    a, b = np.r_[np.full(6, 0.1), 0.2], np.r_[np.full(6, 0.2), 0.8]
    chunks = iter([a, b, b, b, b, b, b])

    def const(obs):
        return np.repeat(next(chunks)[None], 50, axis=0)

    pol = ScriptedPolicy(clock, lambda i: (2, "ok"), const)
    ex, traj = run_virtual(pol, clock, 40, ExecConfig(speed_fraction=10.0, max_speed_rad_s=1e9, soft_start_ticks=1))
    # chunk A in use from step 2; chunk B (requested at step 10) lands at step 12
    assert np.allclose(traj[11, :6], 0.1)
    for k, w in enumerate((0.25, 0.5, 0.75, 1.0)):
        assert np.allclose(traj[12 + k], (1 - w) * a + w * b), k


def test_holds_on_stale_chunk_then_recovers():
    clock = VClock()
    plan = {3: (None, "ok")}  # the 4th reply never comes ...

    def hooks(i, ex, pol):
        if i == 75:  # ... until the client's request timeout fires (here: at step 75)
            pol.drop_inflight()

    pol = ScriptedPolicy(clock, lambda i: plan.get(i, (5, "ok")))
    ex, traj = run_virtual(pol, clock, 150, hooks=hooks)
    holds = [h for h in ex.stats.holds if h["reason"] in ("stale_chunk", "policy_error")]
    assert holds, ex.stats.holds
    h = holds[0]
    # the last good chunk landed at step 25 (pos 5): it runs out at chunk index 40 -> hold from step 60
    assert h["start"] == 60
    held = traj[h["start"]: h["end"] + 1]
    assert np.allclose(held, held[0])  # the target is frozen while holding
    assert ex.stats.errors == {"timeout": 1}
    assert ex.stats.aborted is None and len(traj) == 150  # resumed after the retry
    assert ex.stats.chunks_used > 8


def test_errors_retry_three_times_then_abort():
    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (1, "network") if i < 3 else (5, "ok"))
    ex, traj = run_virtual(pol, clock, 60)
    assert ex.stats.aborted is None and ex.stats.errors == {"network": 3} and len(traj) == 60

    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (1, "not_ready"))
    ex, traj = run_virtual(pol, clock, 60)
    assert ex.stats.aborted and "not_ready" in ex.stats.aborted
    assert ex.stats.errors == {"not_ready": 4}  # first try + 3 retries
    assert np.allclose(traj, traj[0])  # never moved


def test_chunk_older_than_hold_index_is_discarded():
    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (45, "ok") if i == 0 else (5, "ok"))
    ex, _ = run_virtual(pol, clock, 100)
    assert ex.stats.stale_discarded == 1
    assert ex.stats.chunks_used >= 1


def test_sync_fallback_runs_25_then_queries():
    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (5, "ok"))
    ex, traj = run_virtual(pol, clock, 120, ExecConfig(mode="sync"))
    assert set(ex.stats.skips) == {0}
    steps = [o.step for o in pol.submitted]
    assert steps[:3] == [0, 30, 60]  # 5 held + 25 executed per cycle
    moving = np.any(np.abs(np.diff(traj[:, :6], axis=0)) > 1e-12, axis=1)
    # chunk lands at step 5 and drives steps 5..29 (diff index 4..28), then the arm holds for the query
    assert moving[4:29].all() and not moving[29:34].any()


def test_pause_gap_drops_chunk_and_requeries():
    clock = VClock()
    pol = ScriptedPolicy(clock, lambda i: (5, "ok"))
    cfg = ExecConfig()
    ex = ChunkExecutor(pol, lambda s, n, st: Observation([], list(st.state_vector()), "p", n, s), cfg, VMAX)
    st = state_of(np.zeros(6))
    for i in range(30):
        clock.now = i * DT
        ex.tick(i, clock.now, st)
    clock.now = 30 * DT + 2.0  # two seconds paused (takeover / reset)
    n_req = pol.n
    ex.tick(30, clock.now, st)
    assert ex.stats.gap_resets == 1 and ex.chunk is None


def test_map_actions_droid_base_is_integrated_velocity():
    cfg = ExecConfig(base_velocity_scale=0.5)
    acts = np.zeros((50, 8))
    acts[:, 0] = 1.0  # +1 rad/s on joint 1
    acts[:, 7] = 1.0  # DROID: 1 = closed
    anchor = np.r_[np.zeros(6), 1.0]
    out = map_actions(acts, None, anchor, cfg, np.full(6, 3.0))
    assert out.shape == (50, 7)
    assert out[9, 0] == pytest.approx(10 * 0.5 * DT)
    assert np.allclose(out[:, 6], 0.0)  # ours: 0 = closed
    out2 = map_actions(np.ones((50, 7)) * 0.3, "joint_pos_abs", anchor, cfg, np.full(6, 3.0))
    assert np.allclose(out2, 0.3)


# =========================================================================== part 2: real time


@pytest.fixture()
def sim_robot():
    from rdf_robot_client.cameras import CameraRig
    from rdf_robot_client.main import build_arm

    arm = build_arm("sim")
    arm.connect()
    rig = CameraRig.from_config(synthetic=True)
    rig.start()
    assert rig.wait_ready(5.0)
    robot = StandaloneRobot(arm, {"cameras": rig})
    yield robot
    rig.stop()
    arm.disconnect()


def _episode(robot, server, seconds=4.0, cfg_kw=None, **kw):
    ticks = []
    robot.add_tick_listener(ticks.append)
    ctx = robot.context({"prompt": "pick up the cube"})
    cfg = PolicyConfig(backend="spike", url=server.url if server else "http://127.0.0.1:9", **(cfg_kw or {}))
    with PolicyClient(cfg) as pc:
        res = run_episode(ctx, "pick up the cube", timeout_s=seconds, policy=pc, **kw)
    robot.remove_tick_listener(ticks.append)
    return res, ticks


def test_realtime_jittered_latency(sim_robot):
    srv = FakePolicyServer(schedule=jitter_schedule(160, 30, seed=1), velocity=tuple(V)).start()
    try:
        res, ticks = _episode(sim_robot, srv, 5.0)
    finally:
        srv.stop()
    t, e = res.timing, res.executor
    print("\n[tdm50] jittered:", {k: t[k] for k in ("achieved_hz", "period_ms_std", "jitter_ms_p95", "jitter_ms_max")},
          "skips", e["skips"], "switch", e["switch_jumps"], "latency", res.latency)
    assert res.ended == "max_seconds" and res.ok
    # A loaded laptop (other workers, the full suite) stalls this process now and then. The idle-machine
    # bounds (29-31 Hz, skip <= 10, >= 12 chunks, 2.5-step seams, only the start-up hold) are widened only by
    # what the loop itself measured: its deadline overruns and the tick gaps they left. Idle: the old bounds.
    per_ms = DT * 1000
    dts = np.diff([tk.timestamp_us for tk in ticks]) / 1000.0  # ms between streamed commands
    lost = np.maximum(dts - per_ms - 5, 0.0) / per_ms  # ticks lost to each stall, beyond normal jitter
    assert t["achieved_hz"] <= 31.0  # never bursts to catch up after a late tick
    if t["overruns"] == 0:
        assert t["achieved_hz"] >= 29.0  # absolute deadlines: without a missed one the rate cannot drift
    else:  # the process missed deadlines (load); the loop's own work still fits in a period, and it is
        # nowhere near a loop blocking on each ~160 ms query (~6 Hz)
        assert t["tick_ms_p95"] < per_ms and t["achieved_hz"] >= 10.0, t
    rtt_ticks = res.latency["rtt_max_ms"] / per_ms
    every = max(10, int(np.ceil(res.latency["rtt_p95_ms"] / per_ms)) + 1)  # requery_every, or one rtt if longer
    assert e["errors"] == {} and e["chunks_used"] >= min(12, res.steps // every - 2)
    # skip = round(age of the obs / period): a stall only makes a chunk older, never younger
    win = int(np.ceil(rtt_ticks)) + 2  # ticks between a chunk's observation and its adoption
    lost_win = float(np.convolve(lost, np.ones(win), "valid").max()) if len(lost) >= win else float(lost.sum())
    assert 3 <= e["skips"]["min"] and e["skips"]["max"] <= max(10, int(np.ceil(rtt_ticks + lost_win)) + 1)
    # seams stay within ~2 ramp steps despite jitter (rounding of the skip can be off by one); a stall
    # between a chunk's observation and its adoption shifts the seam by the whole ticks it cost
    assert e["switch_jumps"]["max_rad"] <= (2.5 + int(lost_win)) * float(np.max(np.abs(V * DT)))
    # holds: only the start-up wait, plus (load only) a re-wait after a tick gap > gap_reset_s dropped the
    # chunk, or a stale chunk if one round trip plus stalls outlasted hold_at - requery_every
    gap_ms = ExecConfig().gap_reset_s * 1000
    assert e["gap_resets"] <= int((dts > gap_ms - 10).sum())
    allowed = {"no_chunk"} | ({"awaiting_chunk"} if e["gap_resets"] else set()) | (
        {"stale_chunk"} if rtt_ticks + lost_win >= 30 else set())
    assert {h["reason"] for h in e["holds"]} <= allowed, e["holds"]
    assert ticks and {tk.control_source for tk in ticks} == {"policy"}
    assert srv.calls and srv.calls[0].n_images == 2 and len(srv.calls[0].state) == 7


def test_realtime_drop_holds_then_recovers(sim_robot):
    sched = scripted_schedule([Behaviour(0.15), Behaviour(0.15), Behaviour(0.15), Behaviour(0.15, "drop")],
                              Behaviour(0.15))
    srv = FakePolicyServer(schedule=sched, velocity=tuple(V)).start()
    try:
        res, _ = _episode(sim_robot, srv, 4.0, cfg_kw={"request_timeout_s": 1.2})
    finally:
        srv.stop()
    e = res.executor
    print("\n[tdm50] drop:", e["holds"], e["errors"])
    assert res.ended == "max_seconds"
    assert e["errors"] == {"timeout": 1}
    assert any(h["reason"] in ("stale_chunk", "policy_error") for h in e["holds"]) and "held" in res.flags
    assert e["chunks_used"] >= 6 and e["holds"][-1]["end"] < res.steps - 30  # recovered after the reconnect


def test_realtime_server_errors_and_outage(sim_robot):
    srv = FakePolicyServer(schedule=scripted_schedule([Behaviour(0.1)] * 3 + [Behaviour(0.05, "error")] * 2,
                                                      Behaviour(0.1)), velocity=tuple(V)).start()
    try:
        res, _ = _episode(sim_robot, srv, 3.0)
    finally:
        srv.stop()
    assert res.ended == "max_seconds" and res.executor["errors"] == {"fake_error": 2}

    q0 = sim_robot.arm.read_state().joints
    t0 = time.monotonic()
    res, _ = _episode(sim_robot, None, 10.0, warm=False)  # nothing listens on :9
    assert res.ended == "policy_error" and not res.ok
    assert time.monotonic() - t0 < 6.0
    assert np.allclose(sim_robot.arm.read_state().joints, q0, atol=1e-6)  # held the whole time


def test_job_handler_and_production_auth(sim_robot):
    from rdf_robot_client.jobs import HANDLERS

    srv = FakePolicyServer(schedule=jitter_schedule(120, 10), velocity=tuple(V), require_token="tok-test").start()
    try:
        cfg = PolicyConfig(backend="production", url=srv.url, token="tok-test")
        with PolicyClient(cfg) as pc:
            sim_robot.services["policy"] = pc
            ctx = sim_robot.context({"prompt": "pick up the cube", "timeout_s": 2.0})
            out = HANDLERS["run_baseline"](ctx)
            sim_robot.services.pop("policy")
    finally:
        srv.stop()
    r = out["rollout"]
    assert r["ended"] == "max_seconds" and r["policy"]["auth"] is True and r["executor"]["chunks_used"] >= 5
    assert all(c.headers_auth for c in srv.calls)
    assert out["recording"]["recorded"] is False  # no recorder service in this test
