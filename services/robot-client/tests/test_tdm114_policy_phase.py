"""TDM-114: the rollout publishes the policy's phase (and the trial it is on) as ``policy`` on ``/ws/state``.

  * a policy server that labels its chunks ("phase", like the [SIM] scripted policy) -> ``policy.phase`` is that
    label, with the eval runner's trial context (trial_index / trials / purpose / eval_run_id);
  * a server that sends no phase (real pi0.5, the fake server) -> ``policy.phase`` is None, never invented;
  * between trials (robot_state waiting_reset) the phase reads "reset";
  * outside a rollout / trial runner the field is left out.

    cd services/robot-client && uv run --with pytest pytest tests/test_tdm114_policy_phase.py -v
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from test_tdm50_rollout import V, sim_robot  # noqa: F401 - fixture

from rdf_robot_client.fake_policy_server import FakePolicyServer, jitter_schedule
from rdf_robot_client.policy_client import PolicyClient, PolicyConfig
from rdf_robot_client.rollout import POLICY_PHASE_SERVICE, POLICY_TRIAL_SERVICE, run_episode
from rdf_robot_client.stream import LiveStateSampler, _policy_phase

TDM114_PHASES = ("approach", "descend", "close", "lift", "carry")


class Tdm114PhasedServer(FakePolicyServer):
    """The fake ramp server, labelling each chunk with a phase the way sim_policy_server does."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._n = 0

    def _chunk(self, req, b):
        out = super()._chunk(req, b)
        out["phase"] = TDM114_PHASES[self._n % len(TDM114_PHASES)]
        self._n += 1
        return out


def _tdm114_run(robot, server, trial=None):
    sampler = LiveStateSampler(robot, sim=True)
    got: list[dict] = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            got.append(sampler.sample())
            time.sleep(1 / 30)

    th = threading.Thread(target=watch, daemon=True)
    try:
        if trial is not None:
            robot.services[POLICY_TRIAL_SERVICE] = trial
        ctx = robot.context({"prompt": "pick up the cube"})
        th.start()
        with PolicyClient(PolicyConfig(backend="spike", url=server.url)) as pc:
            res = run_episode(ctx, "pick up the cube", timeout_s=2.5, policy=pc)
        time.sleep(0.15)
    finally:
        stop.set()
        th.join(2)
        server.stop()
        robot.services.pop(POLICY_TRIAL_SERVICE, None)
    return res, got, sampler


def test_tdm114_phase_and_trial_on_the_state_feed(sim_robot):  # noqa: F811 - pytest fixture
    srv = Tdm114PhasedServer(schedule=jitter_schedule(120, 10, seed=4), velocity=tuple(V)).start()
    trial = {"trial_index": 2, "trials": 10, "purpose": "baseline", "eval_run_id": 7}
    res, got, sampler = _tdm114_run(sim_robot, srv, trial)
    assert res.ok
    with_phase = [m["policy"] for m in got if (m.get("policy") or {}).get("phase")]
    assert len(with_phase) >= 20, f"{len(with_phase)} of {len(got)} samples carried a phase"
    assert {p["phase"] for p in with_phase} <= set(TDM114_PHASES)
    assert all(p["trial_index"] == 2 and p["trials"] == 10 and p["purpose"] == "baseline" and p["eval_run_id"] == 7
               for p in with_phase)
    # ours only during the episode
    assert POLICY_PHASE_SERVICE not in sim_robot.services
    assert "policy" not in sampler.sample()


def test_tdm114_no_phase_from_a_policy_that_sends_none(sim_robot):  # noqa: F811
    srv = FakePolicyServer(schedule=jitter_schedule(120, 10, seed=5), velocity=tuple(V)).start()
    res, got, _ = _tdm114_run(sim_robot, srv)
    assert res.ok
    pols = [m["policy"] for m in got if "policy" in m]
    assert pols, "the field is there while the rollout runs"
    assert all(p["phase"] is None for p in pols)
    assert all(p["trial_index"] is None and p["trials"] is None for p in pols)  # no trial runner: no trial context


def test_tdm114_reset_between_trials_and_absent_otherwise():
    trial = {"trial_index": 3, "trials": 10, "purpose": "candidate", "eval_run_id": 9, "resetting": True}
    c = SimpleNamespace(services={POLICY_TRIAL_SERVICE: trial})
    assert _policy_phase(c, "waiting_reset") == {"phase": "reset", "trial_index": 3, "trials": 10,
                                                 "purpose": "candidate", "eval_run_id": 9}
    assert _policy_phase(c, "idle")["phase"] is None
    assert _policy_phase(SimpleNamespace(services={}), "waiting_reset") is None
    assert _policy_phase(SimpleNamespace(), "idle") is None
    # a broken callable never drops the state
    bad = SimpleNamespace(services={POLICY_PHASE_SERVICE: lambda: 1 / 0})
    assert _policy_phase(bad, "rollout") is None
