"""TDM-126: the one supervised real-arm deployment run (SimArm behind the SafetyLayer stands in for the YAM)."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from rdf_robot_client.commands import Cancelled
from rdf_robot_client.policy_client import PolicyReply
from rdf_robot_client.rollout import StandaloneRobot
from rdf_robot_client.tdm126_real_run import (
    REAL_RUN_CONFIRM,
    REAL_RUN_PURPOSE,
    RealRunRefused,
    default_checker,
    real_arm_run,
)

URI = "modal-vol://rdf-checkpoints/runs/ticket-14/run-5/2000"
YAM_SPEC = {"action_space": "joint_pos_abs", "state_dim": 7, "action_dim": 7, "joint_dims": 6}


class FakePolicy:
    """In-process production policy: replies at once with a gentle ramp from the observed pose."""

    def __init__(self, *, active: str = URI, backend: str = "production", spec: dict | None = None) -> None:
        self.config = SimpleNamespace(backend=backend, hot_swap=True, describe=lambda: {"backend": backend})
        self.active, self.spec = active, spec or YAM_SPEC
        self.log: list = []
        self.n = 0
        self.pending: list[PolicyReply] = []
        self.observed: list[list[float]] = []

    def wait_healthy(self, timeout: float = 0) -> dict:
        return {"ok": True, "ready": True, "active_checkpoint_uri": self.active, "active": {"spec": self.spec}}

    def _reply(self, obs) -> PolicyReply:
        self.n += 1
        s = np.asarray(obs.state, dtype=float)
        j = np.arange(1, 51)[:, None]
        acts = np.concatenate([s[None, :6] + j * 0.002, np.full((50, 1), 0.3)], axis=1)
        self.observed.append(list(obs.state))
        now = time.monotonic()
        return PolicyReply(self.n, obs.step, obs.t_obs, now, now, actions=acts, action_space="joint_pos_abs")

    def infer_blocking(self, obs, timeout: float = 0) -> PolicyReply:
        return self._reply(obs)

    @property
    def busy(self) -> bool:
        return bool(self.pending)

    def submit(self, obs):
        if self.pending:
            return None
        r = self._reply(obs)
        self.pending.append(r)
        return r.req_id

    def poll(self):
        out, self.pending = self.pending, []
        return out


def ok_checker(*_a):
    return {"verdict": "ok", "problems": []}


@pytest.fixture()
def robot(monkeypatch):
    from rdf_robot_client.main import build_arm

    monkeypatch.setenv("TDM119_ALLOW_POLICY", "1")
    arm = build_arm("sim")
    arm.connect()
    r = StandaloneRobot(arm, {})
    r.ticks = []
    r.add_tick_listener(r.ticks.append)
    yield r
    arm.disconnect()


def payload(**kw):
    return {"purpose": REAL_RUN_PURPOSE, "checkpoint_uri": URI, "prompt": "pick up the red cube", "timeout_s": 1.0,
            "real_arm": True, "confirm": REAL_RUN_CONFIRM, **kw}


def events(robot, name):
    return [p for t, p in robot.events if t == name]


def test_runs_one_capped_rollout_slowly_and_holds(robot):
    scale0 = robot.arm.speed_scale
    pol = FakePolicy()
    ctx = robot.context(payload(speed_scale=0.9))
    seen = {}
    orig = robot.arm.set_speed_scale

    def spy(v):
        seen.setdefault("first", v)
        return orig(v)

    robot.arm.set_speed_scale = spy
    t0 = time.monotonic()
    out = real_arm_run(ctx, checker=ok_checker, policy=pol)
    assert time.monotonic() - t0 < 3.0
    assert out["ok"] and out["moved"] and out["first_chunk_verdict"] == "ok"
    assert out["ended"] in ("max_seconds", "time_cap") and out["seconds"] <= 1.5
    assert seen["first"] <= 0.25 and out["speed_scale"] <= 0.25  # never the payload's 0.9
    assert robot.arm.speed_scale == scale0  # restored
    assert events(robot, "deployment.real_run_first_chunk")[0]["verdict"] == "ok"
    fin = events(robot, "deployment.real_run_finished")
    assert len(fin) == 1 and fin[0]["ok"] is True and "rollout" not in fin[0]
    assert "recording" not in out and "eval_run_id" not in out  # no recorder, no trial rows
    assert len(robot.ticks) > 5


def test_first_chunk_reject_refuses_before_any_motion(robot):
    ctx = robot.context(payload())
    with pytest.raises(RealRunRefused, match="first_chunk_rejected"):
        real_arm_run(ctx, checker=lambda *a: {"verdict": "reject", "problems": ["jump 0.6 rad"]}, policy=FakePolicy())
    assert robot.ticks == []  # nothing was ever streamed to the arm
    fin = events(robot, "deployment.real_run_finished")
    assert fin and fin[0]["refused"] == "first_chunk_rejected" and fin[0]["moved"] is False


@pytest.mark.parametrize("verdict", ["unmapped", "error", None])
def test_anything_but_ok_or_clamp_refuses(robot, verdict):
    with pytest.raises(RealRunRefused):
        real_arm_run(robot.context(payload()), checker=lambda *a: {"verdict": verdict}, policy=FakePolicy())
    assert robot.ticks == []


def test_a_later_rejected_chunk_ends_the_run(robot):
    calls = {"n": 0}

    def checker(*_a):
        calls["n"] += 1
        return {"verdict": "ok" if calls["n"] < 3 else "reject", "problems": ["far"]}

    out = real_arm_run(robot.context(payload(timeout_s=5.0)), checker=checker, policy=FakePolicy())
    assert out["ended"] == "chunk_rejected" and out["ok"] is False and out["rejected"]["verdict"] == "reject"
    assert out["seconds"] < 2.0


@pytest.mark.parametrize("change, code", [
    ({"confirm": None}, "not_confirmed"),
    ({"real_arm": False}, "not_confirmed"),
    ({"checkpoint_uri": "fake://stub-no-gpu/tickets/14/run-5"}, "checkpoint_not_real"),
    ({"checkpoint_uri": "base"}, "checkpoint_not_real"),
    ({"prompt": " "}, "no_prompt"),
])
def test_payload_gates(robot, change, code):
    with pytest.raises(RealRunRefused, match=code):
        real_arm_run(robot.context(payload(**change)), checker=ok_checker, policy=FakePolicy())
    assert robot.ticks == []


def test_env_backend_and_active_checkpoint_gates(robot, monkeypatch):
    with pytest.raises(RealRunRefused, match="backend_not_production"):
        real_arm_run(robot.context(payload()), checker=ok_checker, policy=FakePolicy(backend="sim"))
    with pytest.raises(RealRunRefused, match="checkpoint_not_active"):
        real_arm_run(robot.context(payload()), checker=ok_checker, policy=FakePolicy(active="modal-vol://other/1"))
    with pytest.raises(RealRunRefused, match="checkpoint_not_active"):
        real_arm_run(robot.context(payload()), checker=ok_checker,
                     policy=FakePolicy(spec={"action_space": "droid_base", "action_dim": 8}))
    monkeypatch.setenv("TDM119_ALLOW_POLICY", "0")
    with pytest.raises(RealRunRefused, match="policy_not_allowed"):
        real_arm_run(robot.context(payload()), checker=ok_checker, policy=FakePolicy())
    assert robot.ticks == []


def test_time_cap_is_never_more_than_30s(robot):
    ctx = robot.context(payload(timeout_s=600, confirm="no"))
    with pytest.raises(RealRunRefused):
        real_arm_run(ctx, checker=ok_checker, policy=FakePolicy())
    assert ctx.result["time_cap_s"] == 30.0


def test_soft_estop_stops_it_at_once_and_nothing_moves_after(robot):
    ctx = robot.context(payload(timeout_s=10.0))
    err: dict = {}

    def run():
        try:
            real_arm_run(ctx, checker=ok_checker, policy=FakePolicy())
        except BaseException as e:  # noqa: BLE001
            err["e"], err["t"] = e, time.monotonic()

    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.6)
    n_before = len(robot.ticks)
    assert n_before > 0
    t_stop = time.monotonic()
    robot.arm.stop()  # what the web STOP (soft_estop) and /ws/control {t:"stop"} do: latch the layer + lock
    robot.sm.lock("test e-stop")
    th.join(3.0)
    assert not th.is_alive()
    assert isinstance(err["e"], Cancelled) and err["t"] - t_stop < 0.2
    assert len(robot.ticks) - n_before <= 1
    fin = events(robot, "deployment.real_run_finished")
    assert fin and fin[-1]["ok"] is False


def test_default_checker_rejects_a_far_first_target():
    from rdf_robot_client.tdm122_dryrun import YAM_READY

    chk = default_checker()
    st = list(YAM_READY) + [1.0]
    near = np.array([list(np.array(YAM_READY) + 0.002 * i) + [0.8] for i in range(50)])
    far = near.copy()
    far[:, 1] += 0.6
    assert chk(near, "joint_pos_abs", st)["verdict"] in ("ok", "clamp")
    assert chk(far, "joint_pos_abs", st)["verdict"] == "reject"


def test_run_baseline_routes_the_real_run_purpose(robot, monkeypatch):
    from rdf_robot_client import eval_trials, tdm126_real_run
    from rdf_robot_client.tdm119_teleop import tdm119_handlers

    seen = []
    monkeypatch.setattr(tdm126_real_run, "real_arm_run", lambda ctx: seen.append(ctx.payload) or {"ok": True})
    assert "run_baseline" not in tdm119_handlers(allow_policy=True)  # falls through to the registry handler
    assert eval_trials.run_baseline(robot.context(payload())) == {"ok": True} and seen
